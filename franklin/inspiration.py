import json
import os
import tempfile
from datetime import date, timedelta

_DIR = os.path.dirname(os.path.abspath(__file__))
_INSPIRATION_PATH = os.path.join(_DIR, "inspiration.json")


def _load() -> dict:
    with open(_INSPIRATION_PATH, "r") as f:
        return json.load(f)


def _save(data: dict) -> None:
    dir_ = os.path.dirname(_INSPIRATION_PATH)
    fd, tmp = tempfile.mkstemp(dir=dir_, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, _INSPIRATION_PATH)
    except Exception:
        os.unlink(tmp)
        raise


def _this_week_monday() -> str:
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    return monday.isoformat()


def get_morning_text(virtue_id: str) -> str:
    data = _load()
    entry = data.get(virtue_id, {})
    refresh = entry.get("weekly_refresh")
    if refresh and refresh.get("week_start") == _this_week_monday():
        return refresh["text"]
    return entry.get("base", "")


def set_weekly_refresh(virtue_id: str, week_start: str, text: str) -> None:
    data = _load()
    if virtue_id not in data:
        data[virtue_id] = {}
    data[virtue_id]["weekly_refresh"] = {"week_start": week_start, "text": text}
    _save(data)
