#!/usr/bin/env python3
"""
Pinger — random accountability pings + scheduled reminders + free-form logging.

Backends:
  signal   (default) — uses signal-cli daemon Unix socket
  telegram           — uses Telegram Bot API long-polling

Run:            python pinger.py                   # Signal
                python pinger.py --backend telegram
Test one ping:  python pinger.py --now
Stop:           Ctrl-C

Replying to any bot message counts as confirmation. For random pings you can
also include the [CODE] anywhere in your reply text as a fallback.

reminders.json fields:
  id             — unique string identifier
  message        — text to send
  time           — "HH:MM" when to fire
  days           — "daily" | "weekdays" | "weekends" | ["mon","tue",...]
  repeat_minutes — resend every N min until owner confirms  (default 15)
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
  SIGNAL_GROUP    — base64 group ID from: signal-cli listGroups
  SIGNAL_SOCKET   — path to daemon socket (default /run/signal-cli/socket)

.env keys (Telegram):
  TELEGRAM_TOKEN  — default bot token
  PING_BOT_ID     — separate bot token for pinger DMs (overrides TELEGRAM_TOKEN)

  No chat ID needed — DM the bot once and it auto-registers you.
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
PING_BOT_ID    = os.getenv("PING_BOT_ID")       # separate bot token for pinger; overrides TELEGRAM_TOKEN

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
#
# receive() returns a list of dicts:
#   {"text": str, "reply_to": int | None}
#
# reply_to is the identifier of the message being replied to:
#   Signal   → quote timestamp (ms)
#   Telegram → message_id
#
# send() returns the sent message's own identifier (same type).

class Backend:
    def send(self, text: str) -> int:
        raise NotImplementedError

    def drain(self) -> None:
        raise NotImplementedError

    def receive(self, timeout: float) -> list[dict]:
        raise NotImplementedError


# ── Signal backend ────────────────────────────────────────────────────────────

class _SignalSocket:
    """Persistent JSON-RPC connection to the signal-cli daemon."""

    def __init__(self):
        self._sock     = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        self._sock.connect(SIGNAL_SOCKET)
        self._inbox    = queue.Queue()
        self._pending: dict[int, queue.Queue] = {}
        self._req_id   = 0
        self._lock     = threading.Lock()
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
                        resp_q = self._pending.get(obj["id"])
                        if resp_q:
                            resp_q.put(obj)
                except json.JSONDecodeError:
                    pass

    def send(self, text: str) -> int:
        """Send to group. Returns Signal timestamp (ms) of the sent message."""
        with self._lock:
            self._req_id += 1
            req_id = self._req_id
            resp_q: queue.Queue = queue.Queue()
            self._pending[req_id] = resp_q
            req = {
                "jsonrpc": "2.0",
                "method":  "send",
                "params":  {"groupId": SIGNAL_GROUP, "message": text},
                "id":      req_id,
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


def _parse_signal_message(notification: dict) -> dict | None:
    """Return {"text": str, "reply_to": int | None} if from our group."""
    try:
        envelope = notification["params"]["envelope"]
        data     = envelope.get("dataMessage", {})
        if data.get("groupInfo", {}).get("groupId") != SIGNAL_GROUP:
            return None
        text = data.get("message", "").strip()
        if not text:
            return None
        quote_ts = data.get("quote", {}).get("id")   # ms timestamp of quoted msg
        return {"text": text, "reply_to": quote_ts}
    except (KeyError, TypeError):
        return None


class SignalBackend(Backend):
    def __init__(self):
        self._client = _SignalSocket()

    def send(self, text: str) -> int:
        ts = self._client.send(text)
        print(f"  → {text!r}")
        return ts

    def drain(self) -> None:
        self._client.drain()

    def receive(self, timeout: float) -> list[dict]:
        msgs = []
        for notif in self._client.get_messages(timeout=max(0.0, timeout)):
            msg = _parse_signal_message(notif)
            if msg:
                msgs.append(msg)
        return msgs


# ── Telegram backend ──────────────────────────────────────────────────────────

def _tg_get_owner_chat_id() -> int | None:
    state = load_json(STATE_FILE, {})
    return state.get("owner_chat_id")


def _tg_set_owner_chat_id(chat_id: int) -> None:
    state = load_json(STATE_FILE, {})
    state["owner_chat_id"] = chat_id
    save_json(STATE_FILE, state)
    print(f"  Registered owner chat_id: {chat_id}")


class TelegramBackend(Backend):
    def __init__(self):
        import requests as _req
        self._req    = _req
        token        = PING_BOT_ID or TELEGRAM_TOKEN
        self._base   = f"https://api.telegram.org/bot{token}"
        self._offset = 0

    def _get_updates(self, timeout: int = 30) -> list:
        return self._req.get(
            f"{self._base}/getUpdates",
            params={"timeout": timeout, "offset": self._offset, "limit": 20},
            timeout=timeout + 10,
        ).json().get("result", [])

    def send(self, text: str) -> int:
        chat_id = _tg_get_owner_chat_id()
        if chat_id is None:
            print("  send skipped — no owner registered yet (DM the bot first)")
            return 0
        resp = self._req.post(
            f"{self._base}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=10,
        )
        resp.raise_for_status()
        print(f"  → {text!r}")
        return resp.json()["result"]["message_id"]

    def drain(self) -> None:
        while True:
            updates = self._get_updates(timeout=0)
            if not updates:
                break
            self._offset = updates[-1]["update_id"] + 1

    def receive(self, timeout: float) -> list[dict]:
        if timeout <= 0:
            return []
        try:
            updates = self._get_updates(timeout=min(30, int(timeout)))
        except Exception as e:
            print(f"  poll error: {e}")
            time.sleep(3)
            return []
        msgs = []
        owner = _tg_get_owner_chat_id()
        for upd in updates:
            self._offset = upd["update_id"] + 1
            msg = upd.get("message")
            if not msg:
                continue
            chat_id = msg["chat"]["id"]
            if owner is None:
                _tg_set_owner_chat_id(chat_id)
                owner = chat_id
            elif chat_id != owner:
                continue
            text = msg.get("text", "").strip()
            if not text:
                continue
            reply_to = msg.get("reply_to_message", {}).get("message_id")
            msgs.append({"text": text, "reply_to": reply_to})
        return msgs


# ── Active backend (set in main) ──────────────────────────────────────────────

_backend: Backend | None = None


def send_message(text: str) -> int:
    """Send a message. Returns its identifier for reply detection."""
    return _backend.send(text)


# ── JSON helpers ──────────────────────────────────────────────────────────────

def load_json(path: Path, default):
    return json.loads(path.read_text()) if path.exists() else default


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2))


def load_data() -> dict:
    if not PINGS_FILE.exists():
        return {"pings": [], "days": {}}
    raw = json.loads(PINGS_FILE.read_text())
    if isinstance(raw, list):
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

def _save_log_entry(entry: dict) -> None:
    data = load_data()
    data["days"].setdefault(today_str(), {}).setdefault("log", []).append(entry)
    save_data(data)
    print(f"  Logged: {entry}")


# ── Log follow-up dialog ──────────────────────────────────────────────────────

def _wait_for_choice(options: list[str], timeout: int, sent_id: int) -> str | None:
    """Wait for the first word of any group message to match an option."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for msg in _backend.receive(timeout=min(30.0, deadline - time.time())):
            first = msg["text"].strip().split()[0] if msg["text"].strip() else ""
            if first in options:
                return first
    return None


def _wait_for_input(timeout: int, sent_id: int) -> str | None:
    """Wait for any group message. Returns text or None on timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for msg in _backend.receive(timeout=min(30.0, deadline - time.time())):
            return msg["text"].strip()
    return None


def _parse_duration(text: str) -> dict:
    text = text.strip()
    if ":" in text:
        try:
            h, m   = text.split(":", 1)
            return {"type": "time", "minutes": int(h) * 60 + int(m)}
        except (ValueError, IndexError):
            pass
    else:
        try:
            return {"type": "time", "minutes": int(text)}
        except ValueError:
            pass
    return {"type": "time", "raw": text}


def log_with_followup(text: str) -> None:
    """Log a free-form entry then ask structured follow-up questions."""
    entry: dict = {"ts": now_iso(), "entry": text}

    # ── Quantity ──────────────────────────────────────────────────────────────
    qty_id = send_message(
        f'"{text}"\n\nQuantity?\n1 = None  2 = Ordinal (0–5)  3 = Metric'
    )
    qty = _wait_for_choice(["1", "2", "3"], timeout=180, sent_id=qty_id)

    if qty == "2":
        ord_id = send_message("Rate 0–5:")
        val    = _wait_for_input(timeout=120, sent_id=ord_id)
        if val and val.isdigit() and 0 <= int(val) <= 5:
            entry["quantity"] = {"type": "ordinal", "value": int(val)}
    elif qty == "3":
        met_id = send_message("Quantity (e.g. '2 glasses', '500 ml'):")
        val    = _wait_for_input(timeout=120, sent_id=met_id)
        if val:
            parts = val.split(None, 1)
            if len(parts) == 2:
                try:
                    entry["quantity"] = {"type": "metric", "value": float(parts[0]), "unit": parts[1]}
                except ValueError:
                    entry["quantity"] = {"type": "metric", "raw": val}
            else:
                entry["quantity"] = {"type": "metric", "raw": val}

    # ── Duration ──────────────────────────────────────────────────────────────
    dur_id  = send_message("Duration?\n1 = None  2 = Time")
    dur     = _wait_for_choice(["1", "2"], timeout=180, sent_id=dur_id)

    if dur == "2":
        time_id = send_message("Duration (hr:min or minutes — e.g. '1:30' or '45'):")
        val     = _wait_for_input(timeout=120, sent_id=time_id)
        if val:
            entry["duration"] = _parse_duration(val)

    # ── Result ────────────────────────────────────────────────────────────────
    res_id = send_message("Result?\n1 = None  2 = Ordinal (0–5)")
    res    = _wait_for_choice(["1", "2"], timeout=180, sent_id=res_id)

    if res == "2":
        ord_id = send_message("Rate 0–5:")
        val    = _wait_for_input(timeout=120, sent_id=ord_id)
        if val and val.isdigit() and 0 <= int(val) <= 5:
            entry["result"] = {"type": "ordinal", "value": int(val)}

    # ── Save ──────────────────────────────────────────────────────────────────
    _save_log_entry(entry)
    send_message("✓ Saved")


# ── Poll helpers (backend-agnostic) ───────────────────────────────────────────

def poll_for_yes(timeout_sec: int, sent_id: int) -> bool:
    """
    Wait up to timeout_sec for confirmation.
    Accepts: a reply to sent_id (any text), or any yes-word.
    Other messages are saved as log entries.
    """
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        for msg in _backend.receive(timeout=min(30.0, deadline - time.time())):
            if msg["reply_to"] == sent_id or msg["text"].lower() in YES_WORDS:
                return True
            log_with_followup(msg["text"])
    return False


def poll_for_reply(code: str, sent_at_iso: str, sent_id: int) -> dict:
    """
    Block until confirmed: a reply to sent_id, or a message containing [CODE].
    Other messages are saved as log entries.
    """
    print(f"  Waiting for reply to [{code}] …")
    while True:
        for msg in _backend.receive(timeout=30):
            is_reply  = msg["reply_to"] == sent_id
            has_code  = code in msg["text"].upper()
            if not (is_reply or has_code):
                log_with_followup(msg["text"])
                continue
            replied_at = now_iso()
            elapsed    = round(
                (datetime.fromisoformat(replied_at) - datetime.fromisoformat(sent_at_iso))
                .total_seconds()
            )
            matched_by = "reply" if is_reply else "code"
            entry = {
                "code":                  code,
                "question":              RANDOM_Q,
                "sent_at":               sent_at_iso,
                "replied_at":            replied_at,
                "response_time_seconds": elapsed,
                "answer":                msg["text"],
                "matched_by":            matched_by,
            }
            print(f"  Reply in {elapsed}s ({matched_by}): {msg['text']!r}")
            return entry


def smart_wait(seconds: float, reminders: list) -> str:
    """Wait, logging messages and interrupting if a reminder falls due."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        if get_due_reminder(reminders, load_data(), datetime.now()):
            return "reminder_due"
        for msg in _backend.receive(timeout=min(30.0, deadline - time.time())):
            log_with_followup(msg["text"])
    return "done"


# ── Reminder execution ────────────────────────────────────────────────────────

def run_reminder(reminder: dict) -> None:
    repeat_sec = reminder.get("repeat_minutes", 15) * 60
    record_as  = reminder.get("record_as")
    print(f"[{now_iso()}] Reminder [{reminder['id']}]: {reminder['message']!r}")
    _backend.drain()

    while True:
        sent_id = send_message(reminder["message"])
        if poll_for_yes(timeout_sec=repeat_sec, sent_id=sent_id):
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
    sent_id     = send_message(f"[{code}] {RANDOM_Q}")
    print(f"[{sent_at_iso}] Ping [{code}] sent  id={sent_id}")

    save_json(STATE_FILE, {"pending_code": code, "sent_at_iso": sent_at_iso, "sent_id": sent_id})

    entry = poll_for_reply(code, sent_at_iso, sent_id)
    data  = load_data()
    data["pings"].append(entry)
    save_data(data)
    STATE_FILE.unlink(missing_ok=True)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    global _backend

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--backend", choices=["signal", "telegram"], default="telegram",
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
        if not (PING_BOT_ID or TELEGRAM_TOKEN):
            raise ValueError("PING_BOT_ID or TELEGRAM_TOKEN not set in .env")
        _backend = TelegramBackend()
        token_src = "PING_BOT_ID" if PING_BOT_ID else "TELEGRAM_TOKEN"
        owner = _tg_get_owner_chat_id()
        print(f"Backend: Telegram  |  token: {token_src}  |  owner: {owner or '(waiting for first DM)'}")

    print(f"Random ping window: {WINDOW_START}:00–{WINDOW_END}:00")

    # Resume any pending ping interrupted by a restart
    state = load_json(STATE_FILE, {})
    if state.get("pending_code"):
        code        = state["pending_code"]
        sent_at_iso = state["sent_at_iso"]
        sent_id     = state.get("sent_id", 0)
        print(f"Resuming pending ping [{code}] from {sent_at_iso}")
        entry = poll_for_reply(code, sent_at_iso, sent_id)
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
