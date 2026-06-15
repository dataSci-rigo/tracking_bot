#!/usr/bin/env python3
"""
Pinger — random accountability pings + scheduled reminders + free-form logging.

Reminders are configured in reminders.json. Any message you send to the bot
that isn't a "yes" reply is saved as a timestamped log entry (e.g. "1 beer").

Run:            python pinger.py
Test one ping:  python pinger.py --now     (fires immediately, exits after reply)
Stop:           Ctrl-C

reminders.json fields:
  id             — unique string identifier
  message        — text to send
  time           — "HH:MM" when to fire
  days           — "daily" | "weekdays" | "weekends" | ["mon","tue",...]
  repeat_minutes — resend every N min until owner replies yes  (default 15)
  record_as      — optional key saved in days[date] on confirmation
                   (omit to store under days[date].reminders[id])

pings.json output shape:
  {
    "pings": [...],
    "days": {
      "2026-06-15": {
        "wake_time":  "2026-06-15T07:04:11",   <- from record_as
        "sleep_time": "2026-06-15T23:02:44",
        "reminders":  { "meds": "2026-06-15T08:03:00" },
        "log": [
          { "ts": "2026-06-15T14:30:00", "entry": "1 beer" }
        ]
      }
    }
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

PINGS_FILE     = Path(__file__).parent / "pings.json"
REMINDERS_FILE = Path(__file__).parent / "reminders.json"
STATE_FILE     = Path(__file__).parent / "pinger_state.json"

WINDOW_START = 7    # random pings only fire between 7am …
WINDOW_END   = 23   # … and 11pm
MIN_WAIT_MIN = 30
MAX_WAIT_MIN = 180

RANDOM_Q = "What are you doing?"
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


def load_reminders() -> list:
    return load_json(REMINDERS_FILE, [])


# ── Telegram helpers ──────────────────────────────────────────────────────────

def send_message(text: str) -> tuple[int, int]:
    """Returns (message_id, sent_unix_timestamp)."""
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
    sender = str(msg.get("from", {}).get("id", ""))
    chat   = str(msg["chat"]["id"])
    return sender == str(OWNER_CHAT_ID) or chat == str(OWNER_CHAT_ID)


# ── Timing / reminder helpers ─────────────────────────────────────────────────

def now_iso() -> str:
    return datetime.now().isoformat()


def today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def gen_code() -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=4))


def day_matches(reminder: dict, now: datetime) -> bool:
    days = reminder.get("days", "daily")
    if days in ("daily", ["daily"]):
        return True
    if days == "weekdays":
        return now.weekday() < 5
    if days == "weekends":
        return now.weekday() >= 5
    abbr = now.strftime("%a").lower()   # "mon", "tue", …
    return abbr in days


def reminder_fired_today(reminder: dict, data: dict, today: str) -> bool:
    day = data["days"].get(today, {})
    record_as = reminder.get("record_as")
    if record_as:
        return record_as in day
    return reminder["id"] in day.get("reminders", {})


def get_due_reminder(reminders: list, data: dict, now: datetime):
    """Return the earliest reminder that is due today but not yet confirmed."""
    today = now.strftime("%Y-%m-%d")
    for r in sorted(reminders, key=lambda x: x["time"]):
        if not day_matches(r, now):
            continue
        if reminder_fired_today(r, data, today):
            continue
        h, m = map(int, r["time"].split(":"))
        scheduled = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if now >= scheduled:
            return r
    return None


def next_event_time(reminders: list, data: dict, now: datetime) -> datetime:
    """Earliest future reminder today, or tomorrow's window open."""
    today = now.strftime("%Y-%m-%d")
    for r in sorted(reminders, key=lambda x: x["time"]):
        if not day_matches(r, now):
            continue
        if reminder_fired_today(r, data, today):
            continue
        h, m = map(int, r["time"].split(":"))
        t = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if t > now:
            return t
    tomorrow = now + timedelta(days=1)
    return tomorrow.replace(hour=WINDOW_START, minute=0, second=0, microsecond=0)


# ── Logging ───────────────────────────────────────────────────────────────────

def save_log_entry(text: str) -> None:
    data = load_data()
    entry = {"ts": now_iso(), "entry": text}
    data["days"].setdefault(today_str(), {}).setdefault("log", []).append(entry)
    save_data(data)
    print(f"  Logged: {text!r}")


# ── Poll helpers ──────────────────────────────────────────────────────────────

def poll_for_yes(offset: int, timeout_sec: int) -> tuple[bool, int]:
    """
    Block up to timeout_sec waiting for owner to say yes.
    Any other owner message is saved as a log entry.
    """
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
            text = msg.get("text", "").strip()
            if not text:
                continue
            if text.lower() in YES_WORDS:
                return True, offset
            save_log_entry(text)
    return False, offset


def poll_for_reply(
    code: str,
    sent_message_id: int,
    sent_at_iso: str,
    sent_at_unix: int,
    offset: int,
) -> tuple[dict, int]:
    """
    Block until owner replies to a random ping.
    Unrelated owner messages are saved as log entries.
    """
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
            text     = msg.get("text", "")
            has_code = code in text.upper()
            if not (is_reply or has_code):
                if text.strip():
                    save_log_entry(text.strip())
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
                "answer":                text,
                "matched_by":            "reply" if is_reply else "code",
            }
            print(f"  Reply in {elapsed}s ({entry['matched_by']}): {text!r}")
            return entry, offset

        state = load_json(STATE_FILE, {})
        if state:
            state["update_offset"] = offset
            save_json(STATE_FILE, state)


def smart_wait(seconds: float, reminders: list, offset: int) -> tuple[str, int]:
    """
    Wait up to `seconds`. Returns early with "reminder_due" if a reminder
    falls due. Owner messages that aren't yes-words are logged.
    Returns ("done" | "reminder_due", new_offset).
    """
    deadline = time.time() + seconds
    while time.time() < deadline:
        if get_due_reminder(reminders, load_data(), datetime.now()):
            return "reminder_due", offset

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
            text = msg.get("text", "").strip()
            if text:
                save_log_entry(text)
    return "done", offset


# ── Reminder execution ────────────────────────────────────────────────────────

def run_reminder(reminder: dict, offset: int) -> int:
    repeat_sec = reminder.get("repeat_minutes", 15) * 60
    record_as  = reminder.get("record_as")
    print(f"[{now_iso()}] Reminder [{reminder['id']}]: {reminder['message']!r}")
    offset = drain_updates(offset)

    while True:
        send_message(reminder["message"])
        got_yes, offset = poll_for_yes(offset, timeout_sec=repeat_sec)
        if got_yes:
            confirmed_at = now_iso()
            today        = today_str()
            data         = load_data()
            if record_as:
                data["days"].setdefault(today, {})[record_as] = confirmed_at
            else:
                data["days"].setdefault(today, {}).setdefault("reminders", {})[reminder["id"]] = confirmed_at
            save_data(data)
            print(f"  Confirmed at {confirmed_at}")
            return offset
        print(f"  No reply — repeating in {reminder.get('repeat_minutes', 15)} min")


# ── Random ping ───────────────────────────────────────────────────────────────

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

    print(f"Pinger started. Random ping window: {WINDOW_START}:00–{WINDOW_END}:00")

    offset = 0

    # Resume any pending ping interrupted by a restart
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
        now       = datetime.now()
        data      = load_data()
        reminders = load_reminders()

        # Fire any overdue reminder first
        due = get_due_reminder(reminders, data, now)
        if due:
            offset = run_reminder(due, offset)
            continue

        in_window = WINDOW_START <= now.hour < WINDOW_END

        if not in_window:
            # Sleep until the next reminder or tomorrow's window open
            wake = next_event_time(reminders, data, now)
            wait = max(1.0, (wake - now).total_seconds())
            print(f"[{now_iso()}] Outside window. Sleeping until {wake.strftime('%H:%M')}")
            _, offset = smart_wait(wait, reminders, offset)
            continue

        # Schedule a random ping; cap so we don't overshoot WINDOW_END
        wait_sec   = random.uniform(MIN_WAIT_MIN, MAX_WAIT_MIN) * 60
        window_end = now.replace(hour=WINDOW_END, minute=0, second=0, microsecond=0)
        closes_in  = (window_end - now).total_seconds()
        if wait_sec > closes_in:
            wait_sec = closes_in

        wake_at = now + timedelta(seconds=wait_sec)
        print(f"[{now_iso()}] Next ping at {wake_at.strftime('%H:%M')} ({wait_sec/60:.1f} min)")
        result, offset = smart_wait(wait_sec, reminders, offset)

        if result == "done" and WINDOW_START <= datetime.now().hour < WINDOW_END:
            offset = one_ping(offset)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped. State saved — re-run to resume any pending ping.")
