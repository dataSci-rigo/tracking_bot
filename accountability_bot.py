#!/home/ai1/anaconda3/envs/p312/bin/python3
"""
Accountability Bot
Sends random check-in questions between 7 AM and 11 PM via Telegram.
Stores Q&A sessions and response times in accountability_data.json.
"""

import os
import sys
import json
import time
import random
import datetime
import requests
from pathlib import Path

sys.path.insert(0, "/home/ai1/anaconda3/envs/p312/lib/python3.12/site-packages")
from dotenv import dotenv_values

ENV_PATH = Path(__file__).parent / ".env"
config = dotenv_values(str(ENV_PATH))

TOKEN      = config["TELEGRAM_TOKEN"]
CHAT_ID    = config["GROUP_TRACKING_CHAT_ID"]   # -1003955681692
THREAD_ID  = 4                                   # t.me/c/3955681692/4
OWNER_ID   = int(config["OWNER_CHAT_ID"])        # only accept answers from owner

QUESTIONS = [
    "What are you doing?",
    "What should you be doing?",
    "Are you working hard?",
]

DATA_FILE  = Path(__file__).parent / "accountability_data.json"
API_BASE   = f"https://api.telegram.org/bot{TOKEN}"

WINDOW_START = 7   # 7 AM
WINDOW_END   = 23  # 11 PM

MIN_GAP_SECONDS = 3600       # at least 1 hour between check-ins
MAX_GAP_SECONDS = 9000       # at most 2.5 hours between check-ins


# ── Telegram helpers ────────────────────────────────────────────────────────

def send_message(text: str) -> dict:
    payload = {
        "chat_id": CHAT_ID,
        "message_thread_id": THREAD_ID,
        "text": text,
    }
    r = requests.post(f"{API_BASE}/sendMessage", json=payload, timeout=15)
    r.raise_for_status()
    return r.json()


def drain_updates() -> int:
    """Consume all pending updates and return the next offset to use."""
    r = requests.get(f"{API_BASE}/getUpdates", params={"timeout": 0}, timeout=15)
    r.raise_for_status()
    updates = r.json().get("result", [])
    if updates:
        return updates[-1]["update_id"] + 1
    return 0


def poll_for_reply(offset: int, deadline: float) -> tuple[str | None, float | None, int]:
    """
    Long-poll until we get a text message from OWNER in CHAT_ID/THREAD_ID.
    Returns (answer_text, elapsed_seconds, new_offset).
    Returns (None, None, offset) on timeout.
    """
    start = time.time()
    current_offset = offset

    while time.time() < deadline:
        remaining = int(deadline - time.time())
        poll_timeout = min(30, max(1, remaining))

        try:
            r = requests.get(
                f"{API_BASE}/getUpdates",
                params={"offset": current_offset, "timeout": poll_timeout,
                        "allowed_updates": ["message"]},
                timeout=poll_timeout + 10,
            )
            r.raise_for_status()
        except requests.RequestException as e:
            print(f"  poll error: {e}")
            time.sleep(3)
            continue

        for update in r.json().get("result", []):
            current_offset = update["update_id"] + 1
            msg = update.get("message", {})
            if not msg:
                continue

            # Must be from our chat
            if str(msg.get("chat", {}).get("id")) != CHAT_ID:
                continue

            # Must be from owner
            sender = msg.get("from", {}).get("id")
            if sender != OWNER_ID:
                continue

            # Must be in our thread (or no thread restriction if thread is absent)
            if msg.get("message_thread_id", THREAD_ID) != THREAD_ID:
                continue

            text = msg.get("text", "").strip()
            if text:
                return text, time.time() - start, current_offset

    return None, None, current_offset


# ── Session logic ────────────────────────────────────────────────────────────

def load_data() -> dict:
    if DATA_FILE.exists():
        with open(DATA_FILE) as f:
            return json.load(f)
    return {"sessions": []}


def save_data(data: dict) -> None:
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)


def run_checkin() -> None:
    print(f"[{now_str()}] Starting check-in session")

    session = {
        "timestamp": datetime.datetime.now().isoformat(),
        "questions": [],
    }

    # Drain stale updates so we only capture fresh replies
    offset = drain_updates()

    send_message("Hey! Quick accountability check-in — I have 3 questions for you.")
    time.sleep(0.8)

    for idx, question in enumerate(QUESTIONS, 1):
        resp = send_message(f"{idx}. {question}")
        sent_at = time.time()
        sent_id = resp.get("result", {}).get("message_id", 0)
        print(f"  Q{idx} sent (msg_id={sent_id})")

        deadline = sent_at + 600  # 10 minutes to answer each question
        answer, elapsed, offset = poll_for_reply(offset, deadline)

        if answer is None:
            print(f"  Q{idx}: no response (timeout)")
        else:
            print(f"  Q{idx}: answered in {elapsed:.1f}s → {answer!r}")

        session["questions"].append({
            "question": question,
            "answer": answer,
            "response_time_seconds": round(elapsed, 1) if elapsed is not None else None,
        })

        # Small pause between questions when answered quickly
        if answer and idx < len(QUESTIONS):
            time.sleep(0.8)

    send_message("Thanks for checking in! Stay focused. 💪")
    print(f"[{now_str()}] Session complete")

    data = load_data()
    data["sessions"].append(session)
    save_data(data)


# ── Scheduling ───────────────────────────────────────────────────────────────

def now_str() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def seconds_until_window_open() -> float:
    """Seconds until 7 AM (today if before 7, tomorrow otherwise)."""
    now = datetime.datetime.now()
    target = now.replace(hour=WINDOW_START, minute=0, second=0, microsecond=0)
    if now.hour >= WINDOW_START:
        target += datetime.timedelta(days=1)
    return (target - now).total_seconds()


def window_closes_in() -> float:
    """Seconds until 11 PM today."""
    now = datetime.datetime.now()
    close = now.replace(hour=WINDOW_END, minute=0, second=0, microsecond=0)
    return max(0.0, (close - now).total_seconds())


def main() -> None:
    print("Accountability bot started.")
    print(f"  Chat: {CHAT_ID}  Thread: {THREAD_ID}")
    print(f"  Window: {WINDOW_START}:00 – {WINDOW_END}:00")

    while True:
        now = datetime.datetime.now()

        # Outside active window — sleep until 7 AM
        if now.hour < WINDOW_START or now.hour >= WINDOW_END:
            wait = seconds_until_window_open()
            print(f"[{now_str()}] Outside window. Sleeping {wait/3600:.1f}h until {WINDOW_START}:00")
            time.sleep(wait)
            continue

        # Inside window — wait a random gap then check in
        gap = random.randint(MIN_GAP_SECONDS, MAX_GAP_SECONDS)
        closes_in = window_closes_in()

        if gap > closes_in:
            # Not enough window left today; sleep to tomorrow
            wait = seconds_until_window_open()
            print(f"[{now_str()}] Gap ({gap//60}m) exceeds window. Sleeping until tomorrow.")
            time.sleep(wait)
            continue

        wake_at = now + datetime.timedelta(seconds=gap)
        print(f"[{now_str()}] Next check-in at {wake_at.strftime('%H:%M')} ({gap//60}m from now)")
        time.sleep(gap)

        # Double-check we're still inside window after sleeping
        now2 = datetime.datetime.now()
        if WINDOW_START <= now2.hour < WINDOW_END:
            try:
                run_checkin()
            except Exception as e:
                print(f"[{now_str()}] ERROR during check-in: {e}")


if __name__ == "__main__":
    main()
