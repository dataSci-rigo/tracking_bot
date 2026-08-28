"""
Per-bot on/off state for the merged run_bots.py process.

Each of the five programs (pinger, accountability, franklin, wpi, adhd) can
be individually enabled/disabled at runtime — via the control API
(control_server.py) or the a-bot panel. State persists in bot_state.json so
toggles survive restarts. Missing file/keys default to enabled.

Consumers: run_bots.py's poller (drops + auto-replies for disabled bots'
incoming updates) and each bot's proactive-send guards.
"""
import json
import threading
from pathlib import Path

STATE_FILE = Path(__file__).parent / "bot_state.json"

BOT_NAMES = ["pinger", "accountability", "franklin", "wpi", "adhd"]

_lock = threading.Lock()


def _load() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def enabled(name: str) -> bool:
    return bool(_load().get(name, True))


def set_enabled(name: str, value: bool) -> None:
    if name not in BOT_NAMES:
        raise KeyError(f"Unknown bot: {name}")
    with _lock:
        state = _load()
        state[name] = bool(value)
        STATE_FILE.write_text(json.dumps(state, indent=2))


def all_states() -> dict:
    state = _load()
    return {name: bool(state.get(name, True)) for name in BOT_NAMES}
