#!/usr/bin/env python3
"""
Accountability Bot — random check-ins via Signal.

Schedule (all times America/Los_Angeles):
  07:00        "What's your plan for today?" (once per day, waits up to 30 min)
  07:00–22:00  random 3-question check-ins every 1–2.5 hr
  22:00        "How much did you get done today?" (waits up to 30 min)
  22:00–07:00  silent

Run:   python accountability_bot.py
Test:  python accountability_bot.py --test   (one check-in immediately)

.env keys:
  SIGNAL_NUMBER   — e.g. +13233173769
  SIGNAL_GROUP    — base64 group ID from: signal-cli listGroups
  SIGNAL_SOCKET   — unix socket path (default /run/signal-cli/socket)
"""
import argparse
import json
import os
import queue
import random
import socket as _socket
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

SIGNAL_NUMBER = os.getenv("SIGNAL_NUMBER")
SIGNAL_GROUP  = os.getenv("SIGNAL_GROUP")
SIGNAL_SOCKET = os.getenv("SIGNAL_SOCKET", "/run/signal-cli/socket")

DATA_FILE = Path(__file__).parent / "accountability_data.json"

PT = ZoneInfo("America/Los_Angeles")

WINDOW_START  = 7   # 7 AM PT  — random check-ins begin / morning plan
WINDOW_END    = 22  # 10 PM PT — evening review fires, check-ins stop

MIN_GAP_MIN   = 60
MAX_GAP_MIN   = 150
REPLY_TIMEOUT = 600   # 10 min per question before retrying
MAX_RETRIES   = 2

QUESTIONS = [
    "What are you doing?",
    "What should you be doing?",
    "Are you working hard?",
]


# ── Signal socket ─────────────────────────────────────────────────────────────

class SignalSocket:
    def __init__(self):
        self._sock    = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        self._sock.connect(SIGNAL_SOCKET)
        self._inbox   = queue.Queue()
        self._pending: dict[int, queue.Queue] = {}
        self._req_id  = 0
        self._lock    = threading.Lock()
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self) -> None:
        buf = b""
        while True:
            try:
                chunk = self._sock.recv(65536)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if obj.get("method") == "receive":
                        self._inbox.put(obj)
                    elif "id" in obj:
                        q = self._pending.get(obj["id"])
                        if q:
                            q.put(obj)
                except json.JSONDecodeError:
                    pass

    def send(self, text: str) -> int:
        """Send to group. Returns Signal timestamp (ms)."""
        with self._lock:
            self._req_id += 1
            req_id = self._req_id
            resp_q: queue.Queue = queue.Queue()
            self._pending[req_id] = resp_q
            req = {
                "jsonrpc": "2.0", "method": "send",
                "params": {"groupId": SIGNAL_GROUP, "message": text},
                "id": req_id,
            }
            self._sock.sendall((json.dumps(req) + "\n").encode())
        try:
            resp = resp_q.get(timeout=10)
            return resp.get("result", {}).get("timestamp", 0)
        except queue.Empty:
            return 0
        finally:
            self._pending.pop(req_id, None)

    def drain(self) -> None:
        while not self._inbox.empty():
            try:
                self._inbox.get_nowait()
            except queue.Empty:
                break

    def get_messages(self, timeout: float) -> list[dict]:
        msgs     = []
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            try:
                msgs.append(self._inbox.get(timeout=min(1.0, remaining)))
            except queue.Empty:
                break
        return msgs


_signal: SignalSocket | None = None

def sig() -> SignalSocket:
    global _signal
    if _signal is None:
        _signal = SignalSocket()
    return _signal


def send_message(text: str) -> int:
    ts = sig().send(text)
    print(f"  → {text!r}")
    return ts


def parse_group_message(notification: dict) -> str | None:
    """Return message text if it's from our group, else None."""
    try:
        envelope = notification["params"]["envelope"]
        data     = envelope.get("dataMessage", {})
        if data.get("groupInfo", {}).get("groupId") != SIGNAL_GROUP:
            return None
        return data.get("message", "").strip() or None
    except (KeyError, TypeError):
        return None


def wait_for_any(timeout: float) -> str | None:
    """Block until any group message arrives. Returns text or None on timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for notif in sig().get_messages(timeout=min(30.0, deadline - time.time())):
            text = parse_group_message(notif)
            if text:
                return text
    return None


# ── Time helpers (PT) ─────────────────────────────────────────────────────────

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


# ── Sessions ──────────────────────────────────────────────────────────────────

def morning_session() -> None:
    print(f"[{now_str()}] Morning session")
    sig().drain()
    send_message("Good morning! What's your plan for today?")

    plan = wait_for_any(timeout=30 * 60)

    data = load_data()
    data["days"].setdefault(today_str(), {})["plan"] = plan or "(no response)"
    save_data(data)

    if plan:
        send_message("Got it! Good luck today.")
        print(f"  Plan: {plan!r}")
    else:
        print("  No plan response — moving on")


def evening_session() -> None:
    print(f"[{now_str()}] Evening session")
    sig().drain()
    send_message("How much did you get done today?")

    review = wait_for_any(timeout=30 * 60)

    data = load_data()
    data["days"].setdefault(today_str(), {})["evening_review"] = review or "(no response)"
    save_data(data)

    if review:
        send_message("Nice work today. Rest up!")
        print(f"  Review: {review!r}")
    else:
        print("  No review response — moving on")


def run_checkin() -> None:
    print(f"[{now_str()}] Starting check-in")
    sig().drain()
    send_message("Hey! Quick accountability check-in — 3 questions.")
    time.sleep(0.8)

    collected: list[tuple[str, str]] = []

    for idx, question in enumerate(QUESTIONS, 1):
        answer = None

        for attempt in range(MAX_RETRIES + 1):
            if attempt == 0:
                send_message(f"{idx}. {question}")
            else:
                send_message(f"Still waiting on Q{idx}: {question}")

            print(f"  Q{idx} attempt {attempt + 1}/{MAX_RETRIES + 1}")
            answer = wait_for_any(timeout=REPLY_TIMEOUT)

            if answer:
                print(f"  Q{idx}: {answer!r}")
                break
            print(f"  Q{idx}: timeout")

        if answer is None:
            print(f"[{now_str()}] Dropping session — no response to Q{idx}")
            send_message("No response after 2 attempts. Dropping this check-in — I'll try again later.")
            data = load_data()
            data["sessions"].append({
                "timestamp":           now_pt().isoformat(),
                "status":              "dropped",
                "dropped_on_question": idx,
                "questions": [
                    {"question": q, "answer": None} for q in QUESTIONS
                ],
            })
            save_data(data)
            return

        collected.append((question, answer))
        if idx < len(QUESTIONS):
            time.sleep(0.8)

    send_message("Thanks for checking in! Stay focused.")
    print(f"[{now_str()}] Check-in complete")

    data = load_data()
    data["sessions"].append({
        "timestamp": now_pt().isoformat(),
        "status":    "completed",
        "questions": [{"question": q, "answer": a} for q, a in collected],
    })
    save_data(data)


# ── Main loop ─────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true", help="Run one check-in immediately")
    args = parser.parse_args()

    if not SIGNAL_NUMBER:
        raise ValueError("SIGNAL_NUMBER not set in .env")
    if not SIGNAL_GROUP:
        raise ValueError("SIGNAL_GROUP not set in .env")

    print(f"Accountability bot started. Window: {WINDOW_START}:00–{WINDOW_END}:00 PT")

    if args.test:
        run_checkin()
        return

    while True:
        now   = now_pt()
        today = today_str()
        data  = load_data()

        # Before 7am: sleep until window opens
        if now.hour < WINDOW_START:
            wait = seconds_until_hour_pt(WINDOW_START)
            print(f"[{now_str()}] Before window. Sleeping {wait/3600:.1f}h until {WINDOW_START}:00 PT")
            time.sleep(wait)
            continue

        # 10pm+: evening review then sleep
        if now.hour >= WINDOW_END:
            if "evening_review" not in data["days"].get(today, {}):
                evening_session()
            wait = seconds_until_hour_pt(WINDOW_START)
            print(f"[{now_str()}] Evening done. Sleeping {wait/3600:.1f}h until {WINDOW_START}:00 PT")
            time.sleep(wait)
            continue

        # Morning plan (once per day)
        if "plan" not in data["days"].get(today, {}):
            morning_session()
            continue

        # Random check-in
        gap_sec   = random.randint(MIN_GAP_MIN * 60, MAX_GAP_MIN * 60)
        window_close = now.replace(hour=WINDOW_END, minute=0, second=0, microsecond=0)
        closes_in = (window_close - now).total_seconds()

        if gap_sec > closes_in:
            print(f"[{now_str()}] Gap exceeds window. Waiting until {WINDOW_END}:00 PT")
            time.sleep(closes_in)
            continue

        wake_at = now + timedelta(seconds=gap_sec)
        print(f"[{now_str()}] Next check-in at {wake_at.strftime('%H:%M')} PT ({gap_sec // 60}m)")
        time.sleep(gap_sec)

        if WINDOW_START <= now_pt().hour < WINDOW_END:
            try:
                run_checkin()
            except Exception as e:
                print(f"[{now_str()}] ERROR: {e}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
