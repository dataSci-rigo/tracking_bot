#!/usr/bin/env python3
"""
Accountability Bot — voice-first random check-ins via Telegram channel thread.
Synchronous requests-based (mirrors pinger.py architecture).

Answers are VOICE-ONLY: typed text (other than /commands) is never accepted
as an answer — the bot asks for a voice note instead. Voice notes are
transcribed (OpenRouter/Gemini, handles Telegram's OGG/Opus natively), and
check-in transcripts are parsed by Claude into per-question answers plus a
notes section for anything that didn't fit; the bot reads the parsed answers
back as confirmation.

Schedule (all times America/Los_Angeles):
  07:00        "What's your plan for today?" — voice reply, transcribed
               (waits up to 30 min)
  07:00–23:00  random check-ins every 1–2.5 hr — ONE message listing all
               four questions (what doing / what should / working hard 0-5 /
               on task 0-5); answer all of them in a single voice note sent
               as a *direct reply* to that message. Open for 24h; asked
               once, never re-prompted. A late reply gets "Check-in timed
               out."
  23:00        "How much did you get done today?" — voice reply, transcribed
               (waits up to 30 min), followed by a 3-day rolling stats
               summary (reply rate, effort rate, on-task rate)
  23:00–07:00  silent

Export (for Claude and other bots — payload is self-describing, see _api):
  HTTP:  GET http://localhost:$RUNBOTS_CONTROL_PORT/accountability/export
  CLI:   python accountability_bot.py --export [FILE]

Run:   python accountability_bot.py
Test:  python accountability_bot.py --test   (one check-in immediately)

.env keys:
  PING_BOT_ID            — Telegram bot token
  PINGER_CHANNEL_ID      — e.g. -1003955681692
  ACCOUNTABILITY_THREAD_ID — e.g. 73
  OPEN_ROUTER            — OpenRouter key (voice transcription)
  ANTHROPIC_API_KEY      — Claude (transcript → answers parsing)
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

REPLY_WINDOW_HOURS = 24   # a check-in's voice reply stays valid this long, asked only once

# Voice pipeline
OPENROUTER_KEY   = os.getenv("OPEN_ROUTER", "")
TRANSCRIBE_MODEL = "google/gemini-2.5-flash"   # accepts Telegram's OGG/Opus directly
PARSE_MODEL      = "claude-opus-5"

CHECKIN_QUESTIONS = [
    ("q1",      "Q1", "What are you doing?"),
    ("q2",      "Q2", "What should you be doing?"),
    ("effort",  "Q3", "Are you working hard? (0-5)"),
    ("on_task", "Q3b", "Are you on task? (0-5)"),
]

_BASE_URL = ""
_offset   = 0
_queue: "queue.Queue | None" = None
_voice_hint_last = 0.0   # throttle for the "voice replies only" reminder


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


def _send_tracked(text: str) -> "dict | None":
    """Send a message and return {message_id, sent_at} so a later direct
    voice reply can be matched to it."""
    payload: dict = {"chat_id": CHANNEL_ID, "text": text}
    if THREAD_ID:
        payload["message_thread_id"] = THREAD_ID
    try:
        result = _requests.post(f"{_BASE_URL}/sendMessage", json=payload, timeout=10).json()
        if not result.get("ok"):
            print(f"  send error: {result}")
            return None
        return {"message_id": result["result"]["message_id"],
                "sent_at": now_pt().isoformat()}
    except Exception as e:
        print(f"  send error: {e}")
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


# ── Voice pipeline: download → transcribe → parse ─────────────────────────────

def _download_voice(file_id: str) -> "bytes | None":
    try:
        info = _requests.get(f"{_BASE_URL}/getFile", params={"file_id": file_id}, timeout=15).json()
        if not info.get("ok"):
            print(f"  getFile error: {info}")
            return None
        path = info["result"]["file_path"]
        resp = _requests.get(f"https://api.telegram.org/file/bot{TOKEN}/{path}", timeout=30)
        resp.raise_for_status()
        return resp.content
    except Exception as e:
        print(f"  voice download error: {e}")
        return None


def transcribe_audio(audio_bytes: bytes) -> "str | None":
    """Transcribe a Telegram voice note (OGG/Opus) via OpenRouter/Gemini —
    accepts the ogg format natively, no conversion needed. Returns the
    verbatim transcript, or None on failure/no speech."""
    import base64
    try:
        resp = _requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENROUTER_KEY}"},
            json={
                "model": TRANSCRIBE_MODEL,
                "messages": [{"role": "user", "content": [
                    {"type": "text", "text":
                        "Transcribe this voice note verbatim. Output only the "
                        "transcript, no commentary. If it contains no speech, "
                        "reply exactly: NO_SPEECH"},
                    {"type": "input_audio", "input_audio": {
                        "data": base64.b64encode(audio_bytes).decode(),
                        "format": "ogg"}},
                ]}],
            },
            timeout=90,
        ).json()
        text = resp.get("choices", [{}])[0].get("message", {}).get("content")
        if not text or text.strip() == "NO_SPEECH":
            print(f"  transcription empty/no-speech: {str(resp)[:200]}")
            return None
        return text.strip()
    except Exception as e:
        print(f"  transcription error: {e}")
        return None


def parse_checkin_transcript(transcript: str) -> "dict | None":
    """One Claude call: map a free-form voice transcript onto the four
    check-in questions + a notes section for everything that didn't fit.
    Returns {"q1": str|None, "q2": str|None, "effort": int|None,
    "on_task": int|None, "notes": str|None} or None on failure."""
    import anthropic
    questions_desc = "\n".join(f"- {key}: {prompt}" for key, _, prompt in CHECKIN_QUESTIONS)
    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model=PARSE_MODEL,
            max_tokens=1024,
            system=(
                "You parse a transcript of a spoken accountability check-in "
                "into structured answers. The speaker was asked:\n"
                f"{questions_desc}\n\n"
                "Return ONLY a JSON object with keys q1, q2, effort, on_task, "
                "notes. q1/q2 are concise strings in the speaker's own words "
                "(lightly cleaned up); effort and on_task are integers 0-5 "
                "(map verbal ratings like 'pretty hard' sensibly; if the "
                "speaker gives a number, use it). Use null for any question "
                "the transcript doesn't answer. Put everything meaningful "
                "that didn't fit the four answers into notes (null if "
                "nothing). No markdown fences, no commentary."
            ),
            messages=[{"role": "user", "content": transcript}],
        )
        text = "".join(b.text for b in response.content if b.type == "text").strip()
        if text.startswith("```"):
            text = text.strip("`").lstrip("json").strip()
        parsed = json.loads(text)
        out = {}
        for key in ("q1", "q2", "notes"):
            val = parsed.get(key)
            out[key] = str(val).strip() if val not in (None, "") else None
        for key in ("effort", "on_task"):
            val = parsed.get(key)
            out[key] = max(0, min(5, int(val))) if val is not None else None
        return out
    except Exception as e:
        print(f"  parse error: {e}")
        return None


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


def _log_unanswered_checkin(pending_rec: dict) -> None:
    """Log all four questions of a check-in as unanswered (expiry/timeout)."""
    for _, label, prompt in CHECKIN_QUESTIONS:
        _log_checkin_event({
            "label": label, "prompt": prompt, "kind": "voice",
            "message_id": pending_rec.get("message_id"),
            "sent_at": pending_rec["sent_at"],
            "answered_at": None, "expired": True, "value": None,
        })


def _expire_stale_pending() -> None:
    """Resolve any pending check-in whose 24h reply window has lapsed with
    no reply at all, so its questions still count as unanswered in the
    daily stats instead of lingering forever."""
    pending = load_pending()
    if not pending:
        return
    now = now_pt()
    remaining = []
    for rec in pending:
        sent_at = datetime.fromisoformat(rec["sent_at"])
        if now - sent_at > timedelta(hours=REPLY_WINDOW_HOURS):
            _log_unanswered_checkin(rec)
        else:
            remaining.append(rec)
    if len(remaining) != len(pending):
        save_pending(remaining)


def _voice_only_hint() -> None:
    """Remind (throttled) that answers must be voice notes."""
    global _voice_hint_last
    if time.time() - _voice_hint_last < 300:
        return
    _voice_hint_last = time.time()
    send("🎤 Voice replies only — send a voice note to answer. (Commands like /help still work as text.)")


def _process_voice_checkin(match: dict, msg: dict) -> None:
    """A voice note arrived as a direct reply to the pending check-in:
    enforce the 24h window, transcribe, parse into the four answers + notes,
    log, and read the parsed answers back."""
    pending     = load_pending()
    received_at = now_pt()
    sent_at     = datetime.fromisoformat(match["sent_at"])

    if received_at - sent_at > timedelta(hours=REPLY_WINDOW_HOURS):
        send("Check-in timed out.")
        _log_unanswered_checkin(match)
        save_pending([r for r in pending if r["message_id"] != match["message_id"]])
        return

    file_id = msg.get("voice", {}).get("file_id") or msg.get("audio", {}).get("file_id")
    audio = _download_voice(file_id) if file_id else None
    transcript = transcribe_audio(audio) if audio else None
    if transcript is None:
        send("Couldn't transcribe that voice note — please try again (same reply).")
        return  # keep pending so a retry can match

    parsed = parse_checkin_transcript(transcript)
    if parsed is None:
        send("Transcribed but couldn't parse the answers — please try again (same reply).")
        return  # keep pending so a retry can match

    # Log one event per question (labels Q1/Q2/Q3/Q3b feed the 3-day stats;
    # Q3=effort, Q3b=on-task). A question the voice note didn't cover counts
    # as unanswered.
    for key, label, prompt in CHECKIN_QUESTIONS:
        value = parsed.get(key)
        _log_checkin_event({
            "label": label, "prompt": prompt, "kind": "voice",
            "message_id": match["message_id"], "sent_at": match["sent_at"],
            "answered_at": received_at.isoformat() if value is not None else None,
            "expired": False,
            "value": str(value) if value is not None else None,
        })

    # Store the raw transcript + notes alongside the day's log
    data = load_data()
    day = data["days"].setdefault(match["sent_at"][:10], {})
    day.setdefault("checkin_voice", []).append({
        "sent_at": match["sent_at"],
        "received_at": received_at.isoformat(),
        "transcript": transcript,
        "parsed": parsed,
    })
    save_data(data)
    save_pending([r for r in load_pending() if r["message_id"] != match["message_id"]])

    def fmt_scale(v):
        return f"{v}/5" if v is not None else "—"
    send(
        "✅ Check-in recorded:\n"
        f"1. Doing: {parsed['q1'] or '—'}\n"
        f"2. Should be doing: {parsed['q2'] or '—'}\n"
        f"3. Working hard: {fmt_scale(parsed['effort'])}\n"
        f"4. On task: {fmt_scale(parsed['on_task'])}\n"
        f"📝 Notes: {parsed['notes'] or '—'}"
    )
    print(f"[{now_str()}] Voice check-in processed ({len(transcript)} chars transcript)")


def _poll(timeout: float) -> list[str]:
    """Poll for up to `timeout` seconds. Handles commands, check-in voice
    replies, and the voice-only rule inline. Returns TRANSCRIPTS of voice
    notes that weren't check-in replies — morning/evening's wait_for_reply
    consumes those as its answers (typed text is never an answer)."""
    if timeout <= 0:
        return []
    _expire_stale_pending()
    updates = _get_updates(timeout=min(30, int(timeout)))
    texts = []
    for upd in updates:
        cq = upd.get("callback_query")
        if cq is not None:
            _answer_callback(cq["id"])  # stray tap on an old buttons message
            continue

        msg = upd.get("message")
        if not msg:
            continue
        if msg.get("chat", {}).get("id") != CHANNEL_ID:
            continue
        if THREAD_ID and msg.get("message_thread_id") != THREAD_ID:
            continue

        voice = msg.get("voice") or msg.get("audio")
        if voice:
            reply_to = msg.get("reply_to_message", {}).get("message_id")
            match = next((r for r in load_pending() if r["message_id"] == reply_to), None)
            if match is not None:
                _process_voice_checkin(match, msg)
                continue
            # Voice not tied to a check-in: transcribe for morning/evening waits
            audio_bytes = _download_voice(voice.get("file_id"))
            transcript = transcribe_audio(audio_bytes) if audio_bytes else None
            if transcript:
                texts.append(transcript)
            else:
                send("Couldn't transcribe that voice note — please try again.")
            continue

        text = msg.get("text", "").strip()
        if not text:
            continue
        if text.startswith("/"):
            _handle_command(text)
            continue
        # Typed text is never accepted as an answer
        _voice_only_hint()
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
            "Check-ins are 🎤 voice-only: one message asks all four questions "
            "(doing / should be doing / working hard 0-5 / on task 0-5) — "
            "answer them all in a single voice note sent as a direct reply. "
            "It's transcribed, parsed into answers + notes, and read back. "
            f"Asked once, open for {REPLY_WINDOW_HOURS}h. Morning plan and "
            "evening review are voice notes too. Typed text is never an "
            "answer; commands still work."
        )


# ── Wait for reply ────────────────────────────────────────────────────────────

def wait_for_reply(timeout: float) -> str | None:
    """Block up to `timeout` seconds for one VOICE reply (returns its
    transcript), handling commands along the way. Typed text is never
    accepted as an answer."""
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
    send("Good morning! 🎤 What's your plan for today? (voice note)")
    plan = wait_for_reply(timeout=30 * 60)
    data = load_data()
    data["days"].setdefault(today_str(), {})["plan"] = plan or "(no response)"
    save_data(data)
    if plan:
        send(f"Got it — your plan:\n{plan[:500]}\nGood luck today!")
    else:
        print("  No plan response — moving on")


def evening_session() -> None:
    print(f"[{now_str()}] Evening session")
    send("🎤 How much did you get done today? (voice note)")
    review = wait_for_reply(timeout=30 * 60)
    data = load_data()
    data["days"].setdefault(today_str(), {})["evening_review"] = review or "(no response)"
    save_data(data)
    if review:
        send(f"Recorded:\n{review[:500]}\nNice work today. Rest up!")
    else:
        print("  No review response — moving on")


def start_checkin() -> None:
    """Send ONE message listing all four questions and return immediately —
    the answer is a single voice note sent as a direct reply, resolved
    asynchronously by _poll() (transcribe → parse → read back). Asked once,
    never re-prompted; open for 24h, after which a late reply gets
    "Check-in timed out"."""
    print(f"[{now_str()}] Starting check-in")
    rec = _send_tracked(
        "🎤 Accountability check-in — reply to THIS message with one voice "
        "note answering:\n"
        "1. What are you doing?\n"
        "2. What should you be doing?\n"
        "3. Are you working hard? (0-5)\n"
        "4. Are you on task? (0-5)\n"
        "Anything extra you say goes into Notes."
    )
    if rec is None:
        print(f"[{now_str()}] Check-in send failed")
        return
    pending = load_pending()
    pending.append({**rec, "kind": "voice", "label": "checkin"})
    save_pending(pending)
    print(f"[{now_str()}] Check-in sent; voice reply open for {REPLY_WINDOW_HOURS}h")


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

def _bot_enabled() -> bool:
    """Per-bot on/off toggle (bot_state.py, same dir). Standalone-safe."""
    try:
        import bot_state
        return bot_state.enabled("accountability")
    except Exception:
        return True


def main_loop() -> None:
    while True:
        if not _bot_enabled():
            time.sleep(60)
            continue

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


# ── Export (for Claude and other bots) ────────────────────────────────────────

def build_export() -> dict:
    """Full machine-readable export of the accountability data. The payload
    is self-describing: `_api` documents where to fetch it and what every
    field means, so Claude or any other bot can consume it without reading
    this source file."""
    data = load_data()
    return {
        "_api": {
            "name": "accountability-bot export",
            "version": 2,
            "description": (
                "Complete data export of the accountability bot: daily "
                "morning plans, evening reviews, voice check-ins (transcripts "
                "+ parsed answers), per-question answer log, and 3-day "
                "rolling stats. All timestamps are ISO-8601 in "
                "America/Los_Angeles unless noted."
            ),
            "how_to_fetch": {
                "http": "GET http://localhost:" + os.getenv("RUNBOTS_CONTROL_PORT", "8767")
                        + "/accountability/export  (VM-local; part of the run_bots control server)",
                "cli": "python accountability_bot.py --export [FILE]  (prints JSON to stdout, or writes FILE)",
            },
            "schema": {
                "days": {
                    "<YYYY-MM-DD>": {
                        "plan": "morning plan — transcript of the user's voice reply, or '(no response)'",
                        "evening_review": "evening review — transcript of the user's voice reply, or '(no response)'",
                        "goals": "optional, set via /goals <text>",
                        "summary": "optional, set via /sum <text>",
                        "stats_sent": "bool — the nightly 3-day stats message went out",
                        "checkin_log": [{
                            "label": "Q1|Q2|Q3|Q3b — Q1=what doing, Q2=what should be doing, Q3=working hard 0-5 (effort), Q3b=on task 0-5",
                            "prompt": "the question text as asked",
                            "kind": "voice (current) | text/buttons (historic entries from the pre-voice flow)",
                            "sent_at": "when the check-in was sent",
                            "answered_at": "when the answer arrived, null = unanswered",
                            "expired": "true when the 24h reply window lapsed",
                            "value": "the answer: free text for Q1/Q2, '0'-'5' for Q3/Q3b, null if unanswered",
                        }],
                        "checkin_voice": [{
                            "sent_at": "check-in send time",
                            "received_at": "voice reply time",
                            "transcript": "verbatim transcript of the voice note",
                            "parsed": "{q1, q2, effort, on_task, notes} — Claude's mapping of the transcript onto the questions; notes holds anything that didn't fit",
                        }],
                    },
                },
                "sessions": "legacy pre-2026-07-22 check-in records (question/answer lists)",
                "stats_3day": {
                    "reply_rate": "percent of check-in questions answered over the last 3 days (null if none sent)",
                    "effort_rate": "mean Q3 'working hard' score 0-5 over the last 3 days",
                    "ontask_rate": "mean Q3b 'on task' score 0-5 over the last 3 days",
                },
            },
        },
        "generated_at": now_pt().isoformat(),
        "timezone": "America/Los_Angeles",
        "days": data.get("days", {}),
        "sessions": data.get("sessions", []),
        "stats_3day": compute_3day_stats(),
    }


def main() -> None:
    global _BASE_URL

    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--test", action="store_true", help="Run one check-in immediately then exit")
    parser.add_argument("--export", nargs="?", const="-", metavar="FILE",
                        help="Print the full data export as JSON (or write it to FILE) and exit")
    args = parser.parse_args()

    if args.export is not None:
        doc = json.dumps(build_export(), indent=2)
        if args.export == "-":
            print(doc)
        else:
            Path(args.export).write_text(doc)
            print(f"Export written to {args.export}")
        return

    if not TOKEN:
        raise ValueError("PING_BOT_ID not set in .env")
    if not CHANNEL_ID:
        raise ValueError("PINGER_CHANNEL_ID not set in .env")
    if not THREAD_ID:
        raise ValueError("ACCOUNTABILITY_THREAD_ID not set in .env")

    _BASE_URL = f"https://api.telegram.org/bot{TOKEN}"

    if args.test:
        start_checkin()
        print("Check-in sent — voice-reply in Telegram now. Polling for up to 10 min (Ctrl-C to stop)...")
        smart_sleep(10 * 60)
        return

    print(f"Accountability bot started. Window: {WINDOW_START}:00–{WINDOW_END}:00 PT → thread {THREAD_ID}")
    main_loop()


if __name__ == "__main__":
    main()
