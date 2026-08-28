import os
from pathlib import Path
import yaml
from dotenv import load_dotenv

# todo_list/.env (package lives at todo_list/wpi/)
load_dotenv(Path(__file__).parent.parent / ".env")

# Shared bot token — one poller for all todo_list bots (run_bots.py);
# messages are routed to this bot by Telegram topic (WPI_THREAD_ID).
_raw_token = os.getenv("PING_BOT_ID")
if not _raw_token:
    raise RuntimeError("Set PING_BOT_ID in .env")
TELEGRAM_TOKEN: str = _raw_token

ANTHROPIC_API_KEY: str = os.environ["ANTHROPIC_API_KEY"]

CHANNEL_ID: int = int(os.getenv("PINGER_CHANNEL_ID", "0") or "0")
THREAD_ID: int = int(os.getenv("WPI_THREAD_ID", "0") or "0")

# Legacy DM identity, still used as the user_id key in the cycles table
_raw_owner = os.getenv("OWNER_CHAT_ID", "")
OWNER_CHAT_ID: int = int(_raw_owner.strip("'\""))

TIMEZONE = "America/Los_Angeles"
MORNING_HOUR = 8
EVENING_HOUR = 21

DB_PATH = Path(__file__).parent / "data" / "tracker.db"
PROGRAM_PATH = Path(__file__).parent / "program" / "program.yaml"

SYNTHESIS_MODEL = "claude-sonnet-4-6"


def send_kwargs() -> dict:
    """chat/thread kwargs for proactive sends into the Willpower topic."""
    kwargs: dict = {"chat_id": CHANNEL_ID}
    if THREAD_ID:
        kwargs["message_thread_id"] = THREAD_ID
    return kwargs


def load_program() -> dict:
    with open(PROGRAM_PATH) as f:
        data = yaml.safe_load(f)
    return {w["week_number"]: w for w in data["weeks"]}


def max_week() -> int:
    return max(load_program())
