#!/usr/bin/env python3
"""
Pinger — random accountability pings + scheduled reminders + free-form logging.

Backends:
  signal   (default) — uses signal-cli daemon Unix socket
  telegram           — uses Telegram Bot API long-polling

Run:            python pinger.py                  # Signal
                python pinger.py --backend telegram
Test one ping:  python pinger.py --now
Stop:           Ctrl-C

reminders.json fields:
  id             — unique string identifier
  message        — text to send
  time           — "HH:MM" when to fire
  days           — "daily" | "weekdays" | "weekends" | ["mon","tue",...]
  repeat_minutes — resend every N min until owner replies yes  (default 15)
  record_as      — optional key saved in days[date] on confirmation

pings.json output shape:
  {
    "pings": [...],
    "days": {
      "2026-06-15": {
        "wake_time": "...", "sleep_time": "...",
        "reminders": { "meds": "..." },
        "log": [{ "ts": "...", "entry": "1 beer" }]
      }
    }
  }

.env keys (Signal):
  SIGNAL_NUMBER   — e.g. +13233173769
  SIGNAL_GROUP    — base64 group ID from signal-cli listGroups
  SIGNAL_SOCKET   — path to daemon socket (default /run/signal-cli/socket)

.env keys (Telegram):
  TELEGRAM_TOKEN
  OWNER_CHAT_ID
  PING_CHAT_ID    (defaults to OWNER_CHAT_ID)
  PING_THREAD_ID  (optional topic thread)
"""
import argparse
import json
import os
import queue
import random
import socket as _socket
import string
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Signal env
SIGNAL_NUMBER = os.getenv("SIGNAL_NUMBER")
SIGNAL_GROUP  = os.getenv("SIGNAL_GROUP")
SIGNAL_SOCKET = os.getenv("SIGNAL_SOCKET", "/run/signal-cli/socket")

# Telegram env
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OWNER_CHAT_ID  = os.getenv("OWNER_CHAT_ID")
PING_CHAT_ID   = os.getenv("PING_CHAT_ID", OWNER_CHAT_ID)
PING_THREAD_ID = os.getenv("PING_THREAD_ID")

PINGS_FILE     = Path(__file__).parent / "pings.json"
REMINDERS_FILE = Path(__file__).parent / "reminders.json"
STATE_FILE     = Path(__file__).parent / "pinger_state.json"

WINDOW_START = 7    # random pings only fire between 7am …
WINDOW_END   = 23   # … and 11pm
MIN_WAIT_MIN = 30
MAX_WAIT_MIN = 180

RANDOM_Q  = "What are you doing?"
YES_WORDS = {"yes", "y", "yeah", "yep", "yup"}


# ── Backend abstraction ───────────────────────────────────────────────────────

class Backend:
    """Transport adapter. receive() returns plain text strings from the owner."""

    def send(self, text: str) -> None:
        raise NotImplementedError

    def drain(self) -> None:
        """Discard any queued/pending messages before starting a new poll."""
        raise NotImplementedError

    def receive(self, timeout: float) -> list[str]:
        """Return owner/group messages that arrive within timeout seconds."""
        raise NotImplementedError


# ── Signal backend ────────────────────────────────────────────────────────────

class _SignalSocket:
    """Persistent JSON-RPC connection to the signal-cli daemon."""

    def __init__(self):
        self._sock   = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        self._sock.connect(SIGNAL_SOCKET)
        self._inbox  = queue.Queue()
        self._req_id = 0
        self._lock   = threading.Lock()
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
                except json.JSONDecodeError:
                    pass

    def send(self, text: str) -> None:
        with self._lock:
            self._req_id += 1
            req = {
                "jsonrpc": "2.0",
                "method":  "send",
                "params":  {"groupId": SIGNAL_GROUP, "message": text},
                "id":      self._req_id,
            }
            self._sock.sendall((json.dumps(req) + "\n").encode())

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


def _parse_group_text(notification: dict) -> str | None:
    try:
        envelope = notification["params"]["envelope"]
        data     = envelope.get("dataMessage", {})
        if data.get("groupInfo", {}).get("groupId") != SIGNAL_GROUP:
            return None
        return data.get("message", "").strip() or None
    except (KeyError, TypeError):
        return None


class SignalBackend(Backend):
    def __init__(self):
        self._client = _SignalSocket()

    def send(self, text: str) -> None:
        self._client.send(text)
        print(f"  → {text!r}")

    def drain(self) -> None:
        self._client.drain()

    def receive(self, timeout: float) -> list[str]:
        texts = []
        for notif in self._client.get_messages(timeout=max(0.0, timeout)):
            text = _parse_group_text(notif)
            if text:
                texts.append(text)
        return texts


# ── Telegram backend ──────────────────────────────────────────────────────────

class TelegramBackend(Backend):
    def __init__(self):
        import requests as _req
        self._req    = _req
        self._base   = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
        self._offset = 0

    def _get_updates(self, timeout: int = 30) -> list:
        return self._req.get(
            f"{self._base}/getUpdates",
            params={"timeout": timeout, "offset": self._offset, "limit": 20},
            timeout=timeout + 10,
        ).json().get("result", [])

    def _is_owner(self, msg: dict) -> bool:
        sender = str(msg.get("from", {}).get("id", ""))
        chat   = str(msg["chat"]["id"])
        return sender == str(OWNER_CHAT_ID) or chat == str(OWNER_CHAT_ID)

    def send(self, text: str) -> None:
        payload: dict = {"chat_id": int(PING_CHAT_ID), "text": text}
        if PING_THREAD_ID:
            payload["message_thread_id"] = int(PING_THREAD_ID)
        self._req.post(f"{self._base}/sendMessage", json=payload, timeout=10).raise_for_status()
        print(f"  → {text!r}")

    def drain(self) -> None:
        while True:
            updates = self._get_updates(timeout=0)
            if not updates:
                break
            self._offset = updates[-1]["update_id"] + 1

    def receive(self, timeout: float) -> list[str]:
        if timeout <= 0:
            return []
        try:
            updates = self._get_updates(timeout=min(30, int(timeout)))
        except Exception as e:
            print(f"  poll error: {e}")
            time.sleep(3)
            return []
        texts = []
        for upd in updates:
            self._offset = upd["update_id"] + 1
            msg = upd.get("message")
            if not msg or not self._is_owner(msg):
                continue
            text = msg.get("text", "").strip()
            if text:
                texts.append(text)
        return texts


# ── Active backend (set in main) ──────────────────────────────────────────────

_backend: Backend | None = None


def send_message(text: str) -> None:
    _backend.send(text)


# ── JSON helpers ──────────────────────────────────────────────────────────────

def load_json(path: Path, default):
    return json.loads(path.read_text()) if path.exists() else default


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2))


def load_data() -> dict:
    if not PINGS_FILE.exists():
        return {"pings": [], "days": {}}
    raw = json.loads(PINGS_FILE.read_text())
    if isinstance(raw, list):       # migrate old flat-list format
        return {"pings": raw, "days": {}}
    return raw


def save_data(data: dict) -> None:
    save_json(PINGS_FILE, data)


def load_reminders() -> list:
    return load_json(REMINDERS_FILE, [])


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
    return now.strftime("%a").lower() in days


def reminder_fired_today(reminder: dict, data: dict, today: str) -> bool:
    day       = data["days"].get(today, {})
    record_as = reminder.get("record_as")
    if record_as:
        return record_as in day
    return reminder["id"] in day.get("reminders", {})


def get_due_reminder(reminders: list, data: dict, now: datetime):
    today = now.strftime("%Y-%m-%d")
    for r in sorted(reminders, key=lambda x: x["time"]):
        if not day_matches(r, now):
            continue
        if reminder_fired_today(r, data, today):
            continue
        h, m = map(int, r["time"].split(":"))
        if now >= now.replace(hour=h, minute=m, second=0, microsecond=0):
            return r
    return None


def next_event_time(reminders: list, data: dict, now: datetime) -> datetime:
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
    data  = load_data()
    entry = {"ts": now_iso(), "entry": text}
    data["days"].setdefault(today_str(), {}).setdefault("log", []).append(entry)
    save_data(data)
    print(f"  Logged: {text!r}")


# ── Poll helpers (backend-agnostic) ───────────────────────────────────────────

def poll_for_yes(timeout_sec: int) -> bool:
    """Wait up to timeout_sec for a yes. Other messages are logged."""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        for text in _backend.receive(timeout=min(30.0, deadline - time.time())):
            if text.lower() in YES_WORDS:
                return True
            save_log_entry(text)
    return False


def poll_for_reply(code: str, sent_at_iso: str) -> dict:
    """Block until a message containing [CODE] arrives."""
    print(f"  Waiting for reply to [{code}] …")
    while True:
        for text in _backend.receive(timeout=30):
            if code in text.upper():
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
                }
                print(f"  Reply in {elapsed}s: {text!r}")
                return entry
            save_log_entry(text)


def smart_wait(seconds: float, reminders: list) -> str:
    """Wait, logging messages and returning early if a reminder falls due."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        if get_due_reminder(reminders, load_data(), datetime.now()):
            return "reminder_due"
        for text in _backend.receive(timeout=min(30.0, deadline - time.time())):
            save_log_entry(text)
    return "done"


# ── Reminder execution ────────────────────────────────────────────────────────

def run_reminder(reminder: dict) -> None:
    repeat_sec = reminder.get("repeat_minutes", 15) * 60
    record_as  = reminder.get("record_as")
    print(f"[{now_iso()}] Reminder [{reminder['id']}]: {reminder['message']!r}")
    _backend.drain()

    while True:
        send_message(reminder["message"])
        if poll_for_yes(timeout_sec=repeat_sec):
            confirmed_at = now_iso()
            data         = load_data()
            if record_as:
                data["days"].setdefault(today_str(), {})[record_as] = confirmed_at
            else:
                data["days"].setdefault(today_str(), {}).setdefault("reminders", {})[reminder["id"]] = confirmed_at
            save_data(data)
            print(f"  Confirmed at {confirmed_at}")
            return
        print(f"  No reply — repeating in {reminder.get('repeat_minutes', 15)} min")


# ── Random ping ───────────────────────────────────────────────────────────────

def one_ping() -> None:
    _backend.drain()
    code        = gen_code()
    sent_at_iso = now_iso()
    send_message(f"[{code}] {RANDOM_Q}")
    print(f"[{sent_at_iso}] Ping [{code}] sent")

    save_json(STATE_FILE, {"pending_code": code, "sent_at_iso": sent_at_iso})

    entry = poll_for_reply(code, sent_at_iso)
    data  = load_data()
    data["pings"].append(entry)
    save_data(data)
    STATE_FILE.unlink(missing_ok=True)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    global _backend

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--backend", choices=["signal", "telegram"], default="signal",
        help="Messaging backend (default: signal)",
    )
    parser.add_argument("--now", action="store_true", help="Fire one ping immediately and exit")
    args = parser.parse_args()

    if args.backend == "signal":
        if not SIGNAL_NUMBER:
            raise ValueError("SIGNAL_NUMBER not set in .env")
        if not SIGNAL_GROUP:
            raise ValueError("SIGNAL_GROUP not set in .env")
        _backend = SignalBackend()
        print(f"Backend: Signal  |  socket: {SIGNAL_SOCKET}")
    else:
        if not TELEGRAM_TOKEN:
            raise ValueError("TELEGRAM_TOKEN not set in .env")
        if not OWNER_CHAT_ID:
            raise ValueError("OWNER_CHAT_ID not set in .env")
        _backend = TelegramBackend()
        print(f"Backend: Telegram  |  chat: {PING_CHAT_ID}")

    print(f"Random ping window: {WINDOW_START}:00–{WINDOW_END}:00")

    # Resume any pending ping interrupted by a restart
    state = load_json(STATE_FILE, {})
    if state.get("pending_code"):
        code        = state["pending_code"]
        sent_at_iso = state["sent_at_iso"]
        print(f"Resuming pending ping [{code}] from {sent_at_iso}")
        entry = poll_for_reply(code, sent_at_iso)
        data  = load_data()
        data["pings"].append(entry)
        save_data(data)
        STATE_FILE.unlink(missing_ok=True)
        if args.now:
            return

    if args.now:
        one_ping()
        return

    while True:
        now       = datetime.now()
        data      = load_data()
        reminders = load_reminders()

        due = get_due_reminder(reminders, data, now)
        if due:
            run_reminder(due)
            continue

        in_window = WINDOW_START <= now.hour < WINDOW_END

        if not in_window:
            wake = next_event_time(reminders, data, now)
            wait = max(1.0, (wake - now).total_seconds())
            print(f"[{now_iso()}] Outside window. Sleeping until {wake.strftime('%H:%M')}")
            smart_wait(wait, reminders)
            continue

        wait_sec  = random.uniform(MIN_WAIT_MIN, MAX_WAIT_MIN) * 60
        closes_in = (now.replace(hour=WINDOW_END, minute=0, second=0, microsecond=0) - now).total_seconds()
        if wait_sec > closes_in:
            wait_sec = closes_in

        wake_at = now + timedelta(seconds=wait_sec)
        print(f"[{now_iso()}] Next ping at {wake_at.strftime('%H:%M')} ({wait_sec/60:.1f} min)")
        result = smart_wait(wait_sec, reminders)

        if result == "done" and WINDOW_START <= datetime.now().hour < WINDOW_END:
            one_ping()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
