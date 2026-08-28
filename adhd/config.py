from __future__ import annotations

import os
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()


def _require(key: str) -> str:
    val = os.getenv(key)
    if not val:
        raise RuntimeError(f"Missing required env var: {key}")
    return val


# Shared bot token — one poller for all todo_list bots (run_bots.py);
# messages are routed to this bot by Telegram topic (ADHD_THREAD_ID).
# (The old dedicated ADHD_BOT_ID token now belongs to brain-dump.)
BOT_TOKEN: str = _require("PING_BOT_ID")
CHAT_ID: int | None = int(os.getenv("PINGER_CHANNEL_ID")) if os.getenv("PINGER_CHANNEL_ID") else None
THREAD_ID: int = int(os.getenv("ADHD_THREAD_ID", "0") or "0")


def send_kwargs() -> dict:
    """chat/thread kwargs for sends into the ADHD Tasks topic."""
    kwargs: dict = {"chat_id": CHAT_ID}
    if THREAD_ID:
        kwargs["message_thread_id"] = THREAD_ID
    return kwargs

TZ = ZoneInfo(os.getenv("TIMEZONE", "America/Los_Angeles"))

MORNING_PING_TIME: str = os.getenv("MORNING_PING_TIME", "07:00")
EVENING_PING_TIME: str = os.getenv("EVENING_PING_TIME", "21:00")
DEFAULT_TIMER_MINUTES: int = int(os.getenv("DEFAULT_TIMER_MINUTES", "25"))
SNOOZE_MINUTES: int = int(os.getenv("SNOOZE_MINUTES", "15"))
_SRC_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH: str = os.getenv("DB_PATH", os.path.join(_SRC_DIR, "data", "adhd.db"))


def morning_time() -> tuple[int, int]:
    h, m = MORNING_PING_TIME.split(":")
    return int(h), int(m)


def evening_time() -> tuple[int, int]:
    h, m = EVENING_PING_TIME.split(":")
    return int(h), int(m)
