#!/usr/bin/env python3
"""
Accountability Bot — random check-ins via Telegram channel thread.
Synchronous requests-based (mirrors pinger.py architecture).

Schedule (all times America/Los_Angeles):
  07:00        "What's your plan for today?" (waits up to 30 min)
  07:00–23:00  random check-ins every 1–2.5 hr — Q1/Q2 sent as plain messages,
               Q3 ("Are you working hard?") sent as a message with inline
               0-5 buttons, with a 0-5 "Are you on task?" follow-up (also
               inline buttons) once Q3 is answered. Replies are only
               accepted as *direct replies* (reply_to) to the sent
               message (Q1/Q2) or a *tap of its own buttons* (Q3/Q3b), open
               for 24h; asked once, never re-prompted. A late reply/tap
               after 24h gets "Check-in timed out."
  23:00        "How much did you get done today?" (waits up to 30 min),
               followed by a 3-day rolling stats summary (reply rate, effort
               rate, on-task rate)
  23:00–07:00  silent

Run:   python accountability_bot.py
Test:  python accountability_bot.py --test   (one check-in immediately)

.env keys:
  PING_BOT_ID            — Telegram bot token
  PINGER_CHANNEL_ID      — e.g. -1003955681692
  ACCOUNTABILITY_THREAD_ID — e.g. 73
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import random
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests as _requests
from dotenv import load_dotenv

load_dotenv()

TOKEN      = os.getenv("PING_BOT_ID", "")
CHANNEL_ID = int(os.getenv("PINGER_CHANNEL_ID", "0") or "0")
THREAD_ID  = int(os.getenv("ACCOUNTABILITY_THREAD_ID", "0") or "0")

DATA_FILE           = Path(__file__).parent / "accountability_data.json"
PAUSE_FILE          = Path(__file__).parent / "pause_state.json"
CHECKIN_PENDING_FILE = Path(__file__).parent / "checkin_pending.json"

PT = ZoneInfo("America/Los_Angeles")

WINDOW_START  = 7    # 7 AM PT
WINDOW_END    = 23   # 11 PM PT
MIN_GAP_MIN   = 60
MAX_GAP_MIN   = 150

REPLY_WINDOW_HOURS = 24   # a question's reply stays valid this long, asked only once
SCALE_OPTIONS      = ["0", "1", "2", "3", "4", "5"]

_BASE_URL = ""
_offset   = 0
_queue: "queue.Queue | None" = None


# ── Time helpers ──────────────────────────────────────────────────────────────

def now_pt() -> datetime:
    return datetime.now(tz=PT)

def now_str() -> str:
    return now_pt().strftime("%Y-%m-%d %H:%M:%S")

def today_str() -> str:
    return now_pt().strftime("%Y-%m-%d")

def seconds_until_hour_pt(hour: int) -> float:
    now    = now_pt()
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    return (target - now).total_seconds()


# ── Data helpers ──────────────────────────────────────────────────────────────

def load_data() -> dict:
    if not DATA_FILE.exists():
        return {"sessions": [], "days": {}}
    raw = json.loads(DATA_FILE.read_text())
    if isinstance(raw, list):
        return {"sessions": raw, "days": {}}
    raw.setdefault("days", {})
    return raw

def save_data(data: dict) -> None:
    DATA_FILE.write_text(json.dumps(data, indent=2))


# ── Check-in pending/log helpers ───────────────────────────────────────────────

def load_pending() -> list:
    if not CHECKIN_PENDING_FILE.exists():
        return []
    return json.loads(CHECKIN_PENDING_FILE.read_text())


def save_pending(items: list) -> None:
    CHECKIN_PENDING_FILE.write_text(json.dumps(items, indent=2))


def _log_checkin_event(event: dict) -> None:
    """Record a resolved (answered or expired) question into that day's log,
    keyed off the question's own sent_at date — used by compute_3day_stats()."""
    day = event["sent_at"][:10]
    data = load_data()
    data["days"].setdefault(day, {}).setdefault("checkin_log", []).append(event)
    save_data(data)


# ── Pause helpers ─────────────────────────────────────────────────────────────

def is_paused() -> bool:
    if not PAUSE_FILE.exists():
        return False
    state = json.loads(PAUSE_FILE.read_text())
    resume_at = state.get("resume_at")
    if resume_at and datetime.fromisoformat(resume_at) <= now_pt():
        PAUSE_FILE.unlink(missing_ok=True)
        return False
    return True

def _write_pause(resume_at: datetime) -> None:
    PAUSE_FILE.write_text(json.dumps({
        "paused_at": now_pt().isoformat(),
        "resume_at": resume_at.isoformat(),
    }, indent=2))


# ── Telegram helpers ──────────────────────────────────────────────────────────

def send(text: str) -> None:
    payload: dict = {"chat_id": CHANNEL_ID, "text": text}
    if THREAD_ID:
        payload["message_thread_id"] = THREAD_ID
    try:
        _requests.post(f"{_BASE_URL}/sendMessage", json=payload, timeout=10)
    except Exception as e:
        print(f"  send error: {e}")


def _send_text_question(label: str, prompt: str) -> "dict | None":
    """Send a plain-text check-in question and return a pending-question
    record (message_id captured so a later direct reply can be matched)."""
    payload: dict = {"chat_id": CHANNEL_ID, "text": prompt}
    if THREAD_ID:
        payload["message_thread_id"] = THREAD_ID
    try:
        result = _requests.post(f"{_BASE_URL}/sendMessage", json=payload, timeout=10).json()
        if not result.get("ok"):
            print(f"  send question error: {result}")
            return None
        msg = result["result"]
        return {
            "label": label, "prompt": prompt, "kind": "text",
            "message_id": msg["message_id"],
            "sent_at": now_pt().isoformat(),
        }
    except Exception as e:
        print(f"  send question error: {e}")
        return None


def _send_scale_question(label: str, prompt: str) -> "dict | None":
    """Send a question with inline 0-5 buttons (not a native Telegram poll)."""
    payload: dict = {
        "chat_id": CHANNEL_ID,
        "text": prompt,
        "reply_markup": {
            "inline_keyboard": [[{"text": o, "callback_data": f"scale:{o}"} for o in SCALE_OPTIONS]],
        },
    }
    if THREAD_ID:
        payload["message_thread_id"] = THREAD_ID
    try:
        result = _requests.post(f"{_BASE_URL}/sendMessage", json=payload, timeout=10).json()
        if not result.get("ok"):
            print(f"  send scale question error: {result}")
            return None
        msg = result["result"]
        return {
            "label": label, "prompt": prompt, "kind": "buttons",
            "message_id": msg["message_id"],
            "sent_at": now_pt().isoformat(),
        }
    except Exception as e:
        print(f"  send scale question error: {e}")
        return None


def _answer_callback(callback_query_id: str) -> None:
    try:
        _requests.post(
            f"{_BASE_URL}/answerCallbackQuery",
            json={"callback_query_id": callback_query_id},
            timeout=5,
        )
    except Exception:
        pass


def _get_updates(timeout: int = 30) -> list:
    """When _queue is set (running under bot.py), pull from the shared
    poller's queue instead of polling getUpdates directly — avoids a second
    long-poller on the same bot token. Otherwise poll directly (standalone
    `python accountability_bot.py` runs)."""
    global _offset

    if _queue is not None:
        updates = []
        try:
            updates.append(_queue.get(timeout=timeout))
        except queue.Empty:
            return []
        while True:
            try:
                updates.append(_queue.get_nowait())
            except queue.Empty:
                break
        return updates

    try:
        resp = _requests.get(
            f"{_BASE_URL}/getUpdates",
            params={"timeout": timeout, "offset": _offset, "limit": 100},
            timeout=timeout + 10,
        )
        updates = resp.json().get("result", [])
        if updates:
            _offset = updates[-1]["update_id"] + 1
        return updates
    except Exception as e:
        print(f"  poll error: {e}")
        time.sleep(3)
        return []


def _drain() -> None:
    if _queue is not None:
        while True:
            try:
                _queue.get_nowait()
            except queue.Empty:
                break
        return
    while True:
        updates = _get_updates(timeout=0)
        if not updates:
            break


def _expire_stale_pending() -> None:
    """Resolve any pending question whose 24h reply window has lapsed with
    no reply attempt at all, so it still counts as unanswered in the daily
    stats instead of lingering forever."""
    pending = load_pending()
    if not pending:
        return
    now = now_pt()
    remaining = []
    for q in pending:
        sent_at = datetime.fromisoformat(q["sent_at"])
        if now - sent_at > timedelta(hours=REPLY_WINDOW_HOURS):
            _log_checkin_event({**q, "answered_at": None, "expired": True, "value": None})
        else:
            remaining.append(q)
    if len(remaining) != len(pending):
        save_pending(remaining)


def _handle_text_reply(msg: dict) -> bool:
    """If `msg` is a direct reply to a pending text question (Q1/Q2), resolve
    it (confirmation or timeout) and return True. Otherwise False."""
    reply_to = msg.get("reply_to_message", {}).get("message_id")
    if reply_to is None:
        return False

    pending = load_pending()
    match = next((q for q in pending if q["kind"] == "text" and q["message_id"] == reply_to), None)
    if match is None:
        return False

    text        = msg.get("text", "").strip()
    received_at = now_pt()
    sent_at     = datetime.fromisoformat(match["sent_at"])

    if received_at - sent_at > timedelta(hours=REPLY_WINDOW_HOURS):
        send("Check-in timed out.")
        _log_checkin_event({**match, "answered_at": None, "expired": True, "value": None})
    else:
        send(
            f"{match['label']}: Sent {sent_at.strftime('%H:%M')} "
            f"Received {received_at.strftime('%H:%M')}, Message: {text[:100]}"
        )
        _log_checkin_event({**match, "answered_at": received_at.isoformat(), "expired": False, "value": text})

    save_pending([q for q in pending if not (q["kind"] == "text" and q["message_id"] == reply_to)])
    return True


def _handle_callback_query(cq: dict) -> None:
    """Match an incoming inline-button tap to a pending buttons question (Q3
    or its Q3b follow-up), resolve it, and — if it was Q3 — send the Q3b
    follow-up. Always answers the callback so Telegram clears the button's
    loading spinner."""
    _answer_callback(cq["id"])

    cq_msg = cq.get("message") or {}
    if cq_msg.get("chat", {}).get("id") != CHANNEL_ID:
        return
    if THREAD_ID and cq_msg.get("message_thread_id") != THREAD_ID:
        return

    data = cq.get("data", "")
    if not data.startswith("scale:"):
        return
    value = data.split(":", 1)[1]

    message_id  = cq_msg.get("message_id")
    pending     = load_pending()
    match       = next((q for q in pending if q["kind"] == "buttons" and q["message_id"] == message_id), None)
    if match is None:
        return

    remaining   = [q for q in pending if not (q["kind"] == "buttons" and q["message_id"] == message_id)]
    received_at = now_pt()
    sent_at     = datetime.fromisoformat(match["sent_at"])
    timed_out   = received_at - sent_at > timedelta(hours=REPLY_WINDOW_HOURS)

    if timed_out:
        send("Check-in timed out.")
        _log_checkin_event({**match, "answered_at": None, "expired": True, "value": None})
    else:
        send(
            f"{match['label']}: Sent {sent_at.strftime('%H:%M')} "
            f"Received {received_at.strftime('%H:%M')}, Message: {value}"
        )
        _log_checkin_event({**match, "answered_at": received_at.isoformat(), "expired": False, "value": value})
        if match["label"] == "Q3":
            followup = _send_scale_question("Q3b", "Are you on task?")
            if followup is not None:
                remaining.append(followup)

    save_pending(remaining)


def _poll(timeout: float) -> list[str]:
    """Poll for up to `timeout` seconds. Returns text messages (for morning/
    evening's plain-text wait); handles commands, check-in replies, and
    check-in button taps inline."""
    if timeout <= 0:
        return []
    _expire_stale_pending()
    updates = _get_updates(timeout=min(30, int(timeout)))
    texts = []
    for upd in updates:
        cq = upd.get("callback_query")
        if cq is not None:
            _handle_callback_query(cq)
            continue

        msg = upd.get("message")
        if not msg:
            continue
        if msg.get("chat", {}).get("id") != CHANNEL_ID:
            continue
        if THREAD_ID and msg.get("message_thread_id") != THREAD_ID:
            continue
        text = msg.get("text", "").strip()
        if not text:
            continue
        if text.startswith("/"):
            _handle_command(text)
            continue
        if _handle_text_reply(msg):
            continue
        texts.append(text)
    return texts


def _handle_command(raw: str) -> None:
    parts = raw.split()
    cmd   = parts[0].lstrip("/").split("@")[0].lower()
    args  = parts[1:]

    if cmd == "pause":
        now = now_pt()
        if args:
            try:
                hours     = float(args[0])
                resume_at = now + timedelta(hours=hours)
                _write_pause(resume_at)
                send(f"Paused for {hours:.4g}h (until {resume_at.strftime('%H:%M')} PT). /resume to end early.")
            except ValueError:
                send("Usage: /pause [hours] — e.g. /pause 2")
        else:
            resume_at = now.replace(hour=WINDOW_END, minute=0, second=0, microsecond=0)
            if now >= resume_at:
                resume_at += timedelta(days=1)
            _write_pause(resume_at)
            send(f"Paused for the rest of the day (until {WINDOW_END}:00 PT). Evening review will still run.")

    elif cmd == "resume":
        if is_paused():
            PAUSE_FILE.unlink(missing_ok=True)
            send("Resumed! Check-ins will continue.")
        else:
            send("Bot is not paused.")

    elif cmd == "status":
        if is_paused():
            state = json.loads(PAUSE_FILE.read_text())
            send(f"Paused since {state['paused_at'][:16]}\nResumes at: {state.get('resume_at','?')[:16]}")
        else:
            send("Running normally.")

    elif cmd == "sum":
        summary = " ".join(args)
        if not summary:
            send("Usage: /sum <your summary>")
            return
        data = load_data()
        data["days"].setdefault(today_str(), {})["summary"] = summary
        save_data(data)
        send(f"Summary saved: {summary}")

    elif cmd == "goals":
        goals = " ".join(args)
        if not goals:
            send("Usage: /goals <your goals>")
            return
        data = load_data()
        data["days"].setdefault(today_str(), {})["goals"] = goals
        save_data(data)
        send(f"Goals saved: {goals}")

    elif cmd == "help":
        send(
            "Accountability Bot\n\n"
            "/pause [hours] — pause check-ins\n"
            "/resume — end a pause early\n"
            "/status — show pause state\n"
            "/sum <text> — save a daily summary\n"
            "/goals <text> — save today's goals\n"
            "/help — show this message\n\n"
            f"Schedule (PT): {WINDOW_START}:00 morning plan · check-ins every "
            f"{MIN_GAP_MIN}–{MAX_GAP_MIN} min · {WINDOW_END}:00 evening review + 3-day stats\n\n"
            "Check-ins: Q1/Q2 are plain messages, Q3 is \"Are you working "
            "hard?\" with inline 0-5 buttons, followed by 0-5 buttons for "
            "\"Are you on task?\" once answered. Only direct replies/button "
            f"taps count, asked once, open for {REPLY_WINDOW_HOURS}h."
        )


# ── Wait for reply ────────────────────────────────────────────────────────────

def wait_for_reply(timeout: float) -> str | None:
    """Block up to `timeout` seconds for one text reply, handling commands along the way."""
    _drain()
    deadline = time.time() + timeout
    while time.time() < deadline:
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        msgs = _poll(timeout=min(30.0, remaining))
        if msgs:
            return msgs[0]
    return None


def smart_sleep(seconds: float) -> None:
    """Sleep for `seconds`, handling commands that arrive in the meantime."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        _poll(timeout=min(30.0, remaining))


# ── Sessions ──────────────────────────────────────────────────────────────────

def morning_session() -> None:
    print(f"[{now_str()}] Morning session")
    send("Good morning! What's your plan for today?")
    plan = wait_for_reply(timeout=30 * 60)
    data = load_data()
    data["days"].setdefault(today_str(), {})["plan"] = plan or "(no response)"
    save_data(data)
    if plan:
        send("Got it! Good luck today.")
    else:
        print("  No plan response — moving on")


def evening_session() -> None:
    print(f"[{now_str()}] Evening session")
    send("How much did you get done today?")
    review = wait_for_reply(timeout=30 * 60)
    data = load_data()
    data["days"].setdefault(today_str(), {})["evening_review"] = review or "(no response)"
    save_data(data)
    if review:
        send("Nice work today. Rest up!")
    else:
        print("  No review response — moving on")


def start_checkin() -> None:
    """Fire Q1/Q2 (plain text) and Q3 (0-5 inline buttons) and return
    immediately — replies are resolved asynchronously by _poll() as they
    arrive (or as "Check-in timed out" if a late reply/tap shows up after
    24h). Never re-prompted: each question is sent exactly once."""
    print(f"[{now_str()}] Starting check-in")
    send("Hey! Quick accountability check-in.")

    pending = load_pending()
    for q in (
        _send_text_question("Q1", "What are you doing?"),
        _send_text_question("Q2", "What should you be doing?"),
        _send_scale_question("Q3", "Are you working hard?"),
    ):
        if q is not None:
            pending.append(q)
    save_pending(pending)
    print(f"[{now_str()}] Check-in questions sent; replies open for {REPLY_WINDOW_HOURS}h")


def compute_3day_stats() -> dict:
    """Reply/effort/on-task rates averaged over today + the previous 2 days."""
    data = load_data()
    days = [(now_pt().date() - timedelta(days=i)).isoformat() for i in range(3)]

    total_sent = total_answered = 0
    effort_scores: list[int] = []
    ontask_scores: list[int] = []

    for day in days:
        for event in data["days"].get(day, {}).get("checkin_log", []):
            total_sent += 1
            if not event.get("answered_at"):
                continue
            total_answered += 1
            if event["label"] == "Q3" and event.get("value") is not None:
                effort_scores.append(int(event["value"]))
            elif event["label"] == "Q3b" and event.get("value") is not None:
                ontask_scores.append(int(event["value"]))

    return {
        "reply_rate":  (total_answered / total_sent * 100) if total_sent else None,
        "effort_rate": (sum(effort_scores) / len(effort_scores)) if effort_scores else None,
        "ontask_rate": (sum(ontask_scores) / len(ontask_scores)) if ontask_scores else None,
    }


def send_daily_stats() -> None:
    stats = compute_3day_stats()

    def fmt(value, suffix: str) -> str:
        return f"{value:.1f}{suffix}" if value is not None else "n/a"

    send(
        "3-day check-in stats:\n"
        f"Reply rate: {fmt(stats['reply_rate'], '%')}\n"
        f"Effort rate: {fmt(stats['effort_rate'], '/5')}\n"
        f"On-task rate: {fmt(stats['ontask_rate'], '/5')}"
    )


# ── Main loop ─────────────────────────────────────────────────────────────────

def main_loop() -> None:
    while True:
        now   = now_pt()
        today = today_str()
        data  = load_data()

        if is_paused():
            if now.hour >= WINDOW_END:
                day_entry = data["days"].get(today, {})
                if "evening_review" not in day_entry:
                    evening_session()
                if "stats_sent" not in day_entry:
                    send_daily_stats()
                    data = load_data()
                    data["days"].setdefault(today, {})["stats_sent"] = True
                    save_data(data)
            else:
                smart_sleep(60)
            continue

        if now.hour < WINDOW_START:
            wait = seconds_until_hour_pt(WINDOW_START)
            print(f"[{now_str()}] Before window. Sleeping {wait/3600:.1f}h until {WINDOW_START}:00 PT")
            smart_sleep(wait)
            continue

        if now.hour >= WINDOW_END:
            day_entry = data["days"].get(today, {})
            if "evening_review" not in day_entry:
                evening_session()
            if "stats_sent" not in day_entry:
                send_daily_stats()
                data = load_data()
                data["days"].setdefault(today, {})["stats_sent"] = True
                save_data(data)
            wait = seconds_until_hour_pt(WINDOW_START)
            print(f"[{now_str()}] Evening done. Sleeping {wait/3600:.1f}h until {WINDOW_START}:00 PT")
            smart_sleep(wait)
            continue

        if "plan" not in data["days"].get(today, {}):
            morning_session()
            continue

        gap_sec      = random.randint(MIN_GAP_MIN * 60, MAX_GAP_MIN * 60)
        window_close = now.replace(hour=WINDOW_END, minute=0, second=0, microsecond=0)
        closes_in    = (window_close - now).total_seconds()

        if gap_sec > closes_in:
            print(f"[{now_str()}] Gap exceeds window. Waiting until {WINDOW_END}:00 PT")
            smart_sleep(closes_in)
            continue

        wake_at = now + timedelta(seconds=gap_sec)
        print(f"[{now_str()}] Next check-in at {wake_at.strftime('%H:%M')} PT ({gap_sec // 60}m)")
        smart_sleep(gap_sec)

        if WINDOW_START <= now_pt().hour < WINDOW_END and not is_paused():
            try:
                start_checkin()
            except Exception as e:
                print(f"[{now_str()}] ERROR in check-in: {e}")


# ── Entry point ───────────────────────────────────────────────────────────────

def run(update_queue: "queue.Queue | None" = None) -> None:
    """Entry point for bot.py: run forever in this thread, pulling updates
    from the shared poller's queue instead of polling getUpdates directly
    (avoids a second long-poller on the same bot token)."""
    global _BASE_URL, _queue

    if not TOKEN:
        raise ValueError("PING_BOT_ID not set in .env")
    if not CHANNEL_ID:
        raise ValueError("PINGER_CHANNEL_ID not set in .env")
    if not THREAD_ID:
        raise ValueError("ACCOUNTABILITY_THREAD_ID not set in .env")

    _BASE_URL = f"https://api.telegram.org/bot{TOKEN}"
    _queue    = update_queue

    print(f"Accountability bot started. Window: {WINDOW_START}:00–{WINDOW_END}:00 PT → thread {THREAD_ID}")
    main_loop()


def main() -> None:
    global _BASE_URL

    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true", help="Run one check-in immediately then exit")
    args = parser.parse_args()

    if not TOKEN:
        raise ValueError("PING_BOT_ID not set in .env")
    if not CHANNEL_ID:
        raise ValueError("PINGER_CHANNEL_ID not set in .env")
    if not THREAD_ID:
        raise ValueError("ACCOUNTABILITY_THREAD_ID not set in .env")

    _BASE_URL = f"https://api.telegram.org/bot{TOKEN}"

    if args.test:
        start_checkin()
        print("Check-in sent — reply/vote in Telegram now. Polling for up to 10 min (Ctrl-C to stop)...")
        smart_sleep(10 * 60)
        return

    print(f"Accountability bot started. Window: {WINDOW_START}:00–{WINDOW_END}:00 PT → thread {THREAD_ID}")
    main_loop()


if __name__ == "__main__":
    main()
