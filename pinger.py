#!/usr/bin/env python3
"""
Pinger — random accountability pings with morning wake-up and evening wind-down.

Schedule:
  07:00        "Are you up?" — repeats every 15 min until you reply yes
  07:00–23:00  random pings every 30–180 min: "What are you doing?"
  23:00        "Have you brushed your teeth?" — repeats every 15 min until you reply yes

Run:            python pinger.py
Test one ping:  python pinger.py --now     (fires immediately, exits after reply)
Stop:           Ctrl-C  (pending ping state is saved and resumed on next start)

Output: pings.json
  {
    "pings": [...],
    "days":  { "2026-06-15": { "wake_time": "...", "sleep_time": "..." } }
  }

.env keys:
  TELEGRAM_TOKEN
  OWNER_CHAT_ID
  PING_CHAT_ID      (defaults to OWNER_CHAT_ID)
  PING_THREAD_ID    (optional topic thread)
"""
import argparse
import json
import os
import random
import string
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

TOKEN          = os.getenv("TELEGRAM_TOKEN")
OWNER_CHAT_ID  = os.getenv("OWNER_CHAT_ID")
PING_CHAT_ID   = os.getenv("PING_CHAT_ID", OWNER_CHAT_ID)
PING_THREAD_ID = os.getenv("PING_THREAD_ID")

BASE = f"https://api.telegram.org/bot{TOKEN}"

PINGS_FILE = Path(__file__).parent / "pings.json"
STATE_FILE = Path(__file__).parent / "pinger_state.json"

WINDOW_START     = 7    # 7 AM
WINDOW_END       = 23   # 11 PM
MIN_WAIT_MIN     = 30
MAX_WAIT_MIN     = 180
CHECKIN_INTERVAL = 15 * 60  # seconds between repeated morning/evening prompts

RANDOM_Q  = "What are you doing?"
MORNING_Q = "Are you up?"
EVENING_Q = "Have you brushed your teeth?"

YES_WORDS = {"yes", "y", "yeah", "yep", "yup"}


# ── JSON helpers ──────────────────────────────────────────────────────────────

def load_json(path: Path, default):
    return json.loads(path.read_text()) if path.exists() else default


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2))


def load_data() -> dict:
    if not PINGS_FILE.exists():
        return {"pings": [], "days": {}}
    raw = json.loads(PINGS_FILE.read_text())
    if isinstance(raw, list):           # migrate old flat-list format
        return {"pings": raw, "days": {}}
    return raw


def save_data(data: dict) -> None:
    save_json(PINGS_FILE, data)


# ── Telegram helpers ──────────────────────────────────────────────────────────

def send_message(text: str) -> tuple[int, int]:
    """Send a message. Returns (message_id, sent_unix_timestamp)."""
    payload: dict = {"chat_id": int(PING_CHAT_ID), "text": text}
    if PING_THREAD_ID:
        payload["message_thread_id"] = int(PING_THREAD_ID)
    resp = requests.post(f"{BASE}/sendMessage", json=payload, timeout=10)
    resp.raise_for_status()
    result = resp.json()["result"]
    return result["message_id"], result["date"]


def get_updates(offset: int, timeout: int = 30) -> list:
    return requests.get(
        f"{BASE}/getUpdates",
        params={"timeout": timeout, "offset": offset, "limit": 20},
        timeout=timeout + 10,
    ).json().get("result", [])


def drain_updates(offset: int) -> int:
    while True:
        updates = get_updates(offset, timeout=0)
        if not updates:
            return offset
        offset = updates[-1]["update_id"] + 1


def is_from_owner(msg: dict) -> bool:
    sender_id = str(msg.get("from", {}).get("id", ""))
    chat_id   = str(msg["chat"]["id"])
    return sender_id == str(OWNER_CHAT_ID) or chat_id == str(OWNER_CHAT_ID)


# ── Timing helpers ────────────────────────────────────────────────────────────

def now_iso() -> str:
    return datetime.now().isoformat()


def today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def seconds_until_hour(hour: int) -> float:
    now    = datetime.now()
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def gen_code() -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=4))


# ── Poll helpers ──────────────────────────────────────────────────────────────

def poll_for_yes(offset: int, timeout_sec: int) -> tuple[bool, int]:
    """Long-poll until owner sends a yes-word or timeout expires."""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        remaining    = int(deadline - time.time())
        poll_timeout = min(30, max(1, remaining))
        try:
            updates = get_updates(offset, timeout=poll_timeout)
        except Exception as e:
            print(f"  poll error: {e}")
            time.sleep(3)
            continue
        for upd in updates:
            offset = upd["update_id"] + 1
            msg = upd.get("message")
            if not msg or not is_from_owner(msg):
                continue
            if msg.get("text", "").strip().lower() in YES_WORDS:
                return True, offset
    return False, offset


def poll_for_reply(
    code: str,
    sent_message_id: int,
    sent_at_iso: str,
    sent_at_unix: int,
    offset: int,
) -> tuple[dict, int]:
    """Block until owner replies to a ping. Returns (log_entry, new_offset)."""
    print(f"  Waiting for reply to [{code}] …")
    while True:
        updates = get_updates(offset)
        for upd in updates:
            offset = upd["update_id"] + 1
            msg = upd.get("message")
            if not msg or msg["date"] <= sent_at_unix:
                continue
            if not is_from_owner(msg):
                continue
            is_reply = msg.get("reply_to_message", {}).get("message_id") == sent_message_id
            has_code = code in msg.get("text", "").upper()
            if not (is_reply or has_code):
                continue
            replied_at = now_iso()
            elapsed    = round(
                (datetime.fromisoformat(replied_at) - datetime.fromisoformat(sent_at_iso))
                .total_seconds()
            )
            entry = {
                "code":                  code,
                "question":              RANDOM_Q,
                "sent_at":               sent_at_iso,
                "replied_at":            replied_at,
                "response_time_seconds": elapsed,
                "answer":                msg.get("text", ""),
                "matched_by":            "reply" if is_reply else "code",
            }
            print(f"  Reply in {elapsed}s ({entry['matched_by']}): {msg.get('text')!r}")
            return entry, offset

        state = load_json(STATE_FILE, {})
        if state:
            state["update_offset"] = offset
            save_json(STATE_FILE, state)


# ── Checkins ──────────────────────────────────────────────────────────────────

def morning_checkin(offset: int) -> int:
    print(f"[{now_iso()}] Morning checkin")
    offset = drain_updates(offset)
    while True:
        send_message(MORNING_Q)
        print(f"  Sent: {MORNING_Q!r}")
        got_yes, offset = poll_for_yes(offset, timeout_sec=CHECKIN_INTERVAL)
        if got_yes:
            wake_time = now_iso()
            print(f"  Awake! Wake time: {wake_time}")
            data = load_data()
            data["days"].setdefault(today_str(), {})["wake_time"] = wake_time
            save_data(data)
            return offset
        print("  No reply — asking again in 15 min")


def evening_checkin(offset: int) -> int:
    print(f"[{now_iso()}] Evening checkin")
    offset = drain_updates(offset)
    while True:
        send_message(EVENING_Q)
        print(f"  Sent: {EVENING_Q!r}")
        got_yes, offset = poll_for_yes(offset, timeout_sec=CHECKIN_INTERVAL)
        if got_yes:
            sleep_time = now_iso()
            print(f"  Sleep time: {sleep_time}")
            data = load_data()
            data["days"].setdefault(today_str(), {})["sleep_time"] = sleep_time
            save_data(data)
            return offset
        print("  No reply — asking again in 15 min")


# ── Regular ping ──────────────────────────────────────────────────────────────

def one_ping(offset: int) -> int:
    offset          = drain_updates(offset)
    code            = gen_code()
    sent_at_iso     = now_iso()
    sent_message_id, sent_at_unix = send_message(f"[{code}] {RANDOM_Q}")
    print(f"[{sent_at_iso}] Ping [{code}] sent  id={sent_message_id}")

    save_json(STATE_FILE, {
        "pending_code":    code,
        "sent_message_id": sent_message_id,
        "sent_at_iso":     sent_at_iso,
        "sent_at_unix":    sent_at_unix,
        "update_offset":   offset,
    })

    entry, offset = poll_for_reply(code, sent_message_id, sent_at_iso, sent_at_unix, offset)

    data = load_data()
    data["pings"].append(entry)
    save_data(data)
    STATE_FILE.unlink(missing_ok=True)
    return offset


# ── Main loop ─────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--now", action="store_true", help="Fire one ping immediately and exit")
    args = parser.parse_args()

    if not TOKEN:
        raise ValueError("TELEGRAM_TOKEN not set in .env")
    if not OWNER_CHAT_ID:
        raise ValueError("OWNER_CHAT_ID not set in .env")

    print(f"Pinger started. Window: {WINDOW_START}:00–{WINDOW_END}:00")
    print(f"Ping destination: chat={PING_CHAT_ID}" + (f" thread={PING_THREAD_ID}" if PING_THREAD_ID else ""))

    offset = 0

    # Resume a pending ping that was interrupted
    state = load_json(STATE_FILE, {})
    if state.get("pending_code"):
        code            = state["pending_code"]
        sent_message_id = state["sent_message_id"]
        sent_at_iso     = state["sent_at_iso"]
        sent_at_unix    = state["sent_at_unix"]
        offset          = state.get("update_offset", 0)
        print(f"Resuming pending ping [{code}] from {sent_at_iso}")
        entry, offset = poll_for_reply(code, sent_message_id, sent_at_iso, sent_at_unix, offset)
        data = load_data()
        data["pings"].append(entry)
        save_data(data)
        STATE_FILE.unlink(missing_ok=True)
        if args.now:
            return

    if args.now:
        one_ping(offset)
        return

    while True:
        now   = datetime.now()
        today = today_str()
        data  = load_data()

        # Before 7am — sleep until window opens
        if now.hour < WINDOW_START:
            wait = seconds_until_hour(WINDOW_START)
            print(f"[{now_iso()}] Outside window. Sleeping {wait/3600:.1f}h until {WINDOW_START}:00")
            time.sleep(wait)
            continue

        # After 11pm — evening checkin then sleep until tomorrow
        if now.hour >= WINDOW_END:
            if "sleep_time" not in data["days"].get(today, {}):
                offset = evening_checkin(offset)
            wait = seconds_until_hour(WINDOW_START)
            print(f"[{now_iso()}] Window closed. Sleeping {wait/3600:.1f}h until {WINDOW_START}:00")
            time.sleep(wait)
            continue

        # Morning checkin — once per day, first thing after window opens
        if "wake_time" not in data["days"].get(today, {}):
            offset = morning_checkin(offset)
            continue

        # Schedule next random ping; if it overshoots 11pm wait for evening instead
        wait_sec      = random.uniform(MIN_WAIT_MIN, MAX_WAIT_MIN) * 60
        closes_in_sec = seconds_until_hour(WINDOW_END)
        if wait_sec >= closes_in_sec:
            print(f"[{now_iso()}] Ping would overshoot window. Waiting until {WINDOW_END}:00")
            time.sleep(closes_in_sec)
            continue

        wake_at = datetime.now() + timedelta(seconds=wait_sec)
        print(f"[{now_iso()}] Next ping at {wake_at.strftime('%H:%M')} ({wait_sec/60:.1f} min)")
        time.sleep(wait_sec)

        if WINDOW_START <= datetime.now().hour < WINDOW_END:
            offset = one_ping(offset)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped. State saved — re-run to resume any pending ping.")
