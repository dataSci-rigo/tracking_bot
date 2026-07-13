#!/usr/bin/env python3
"""
Accountability Bot — random check-ins via Telegram channel thread.
Synchronous requests-based (mirrors pinger.py architecture).

Schedule (all times America/Los_Angeles):
  07:00        "What's your plan for today?" (waits up to 30 min)
  07:00–23:00  random 3-question check-ins every 1–2.5 hr
  23:00        "How much did you get done today?" (waits up to 30 min)
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

DATA_FILE  = Path(__file__).parent / "accountability_data.json"
PAUSE_FILE = Path(__file__).parent / "pause_state.json"

PT = ZoneInfo("America/Los_Angeles")

WINDOW_START  = 7    # 7 AM PT
WINDOW_END    = 23   # 11 PM PT
MIN_GAP_MIN   = 60
MAX_GAP_MIN   = 150
REPLY_TIMEOUT = 600  # 10 min per question
MAX_RETRIES   = 2

QUESTIONS = [
    "What are you doing?",
    "What should you be doing?",
    "Are you working hard?",
]

_BASE_URL = ""
_offset   = 0


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


def _get_updates(timeout: int = 30) -> list:
    global _offset
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
    while True:
        updates = _get_updates(timeout=0)
        if not updates:
            break


def _poll(timeout: float) -> list[str]:
    """Poll for up to `timeout` seconds. Returns text messages; handles commands inline."""
    if timeout <= 0:
        return []
    updates = _get_updates(timeout=min(30, int(timeout)))
    texts = []
    for upd in updates:
        msg = upd.get("message")
        if not msg:
            continue
        if msg.get("chat", {}).get("id") != CHANNEL_ID:
            continue
        text = msg.get("text", "").strip()
        if not text:
            continue
        if text.startswith("/"):
            _handle_command(text)
        else:
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
            f"{MIN_GAP_MIN}–{MAX_GAP_MIN} min · {WINDOW_END}:00 evening review"
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


def run_checkin() -> None:
    print(f"[{now_str()}] Starting check-in")
    send("Hey! Quick accountability check-in — 3 questions.")
    time.sleep(0.8)

    collected: list[tuple[str, str]] = []

    for idx, question in enumerate(QUESTIONS, 1):
        answer = None
        for attempt in range(MAX_RETRIES + 1):
            if attempt == 0:
                send(f"{idx}. {question}")
            else:
                send(f"Still waiting on Q{idx}: {question}")
            answer = wait_for_reply(timeout=REPLY_TIMEOUT)
            if answer:
                break

        if answer is None:
            send("No response after 2 attempts. Dropping this check-in — I'll try again later.")
            data = load_data()
            data["sessions"].append({
                "timestamp":           now_pt().isoformat(),
                "status":              "dropped",
                "dropped_on_question": idx,
                "questions": [{"question": q, "answer": None} for q in QUESTIONS],
            })
            save_data(data)
            return

        collected.append((question, answer))
        if idx < len(QUESTIONS):
            time.sleep(0.8)

    send("Thanks for checking in! Stay focused.")
    print(f"[{now_str()}] Check-in complete")
    data = load_data()
    data["sessions"].append({
        "timestamp": now_pt().isoformat(),
        "status":    "completed",
        "questions": [{"question": q, "answer": a} for q, a in collected],
    })
    save_data(data)


# ── Main loop ─────────────────────────────────────────────────────────────────

def main_loop() -> None:
    while True:
        now   = now_pt()
        today = today_str()
        data  = load_data()

        if is_paused():
            if now.hour >= WINDOW_END and "evening_review" not in data["days"].get(today, {}):
                evening_session()
            else:
                smart_sleep(60)
            continue

        if now.hour < WINDOW_START:
            wait = seconds_until_hour_pt(WINDOW_START)
            print(f"[{now_str()}] Before window. Sleeping {wait/3600:.1f}h until {WINDOW_START}:00 PT")
            smart_sleep(wait)
            continue

        if now.hour >= WINDOW_END:
            if "evening_review" not in data["days"].get(today, {}):
                evening_session()
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
                run_checkin()
            except Exception as e:
                print(f"[{now_str()}] ERROR in check-in: {e}")


# ── Entry point ───────────────────────────────────────────────────────────────

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
        run_checkin()
        return

    print(f"Accountability bot started. Window: {WINDOW_START}:00–{WINDOW_END}:00 PT → thread {THREAD_ID}")
    main_loop()


if __name__ == "__main__":
    main()
