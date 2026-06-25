import json
import os
import tempfile
from datetime import date, timedelta

_DIR = os.path.dirname(os.path.abspath(__file__))
_CONFIG_PATH = os.path.join(_DIR, "config.json")
_DATA_PATH = os.path.join(_DIR, "data.json")


def load_config() -> dict:
    with open(_CONFIG_PATH, "r") as f:
        return json.load(f)


_DATA_DEFAULT = {"marks": {}, "todos": [], "notes": [], "focus_history": [], "next_id": 1}


def _ensure_data_file() -> None:
    if not os.path.exists(_DATA_PATH):
        with open(_DATA_PATH, "w") as f:
            json.dump(_DATA_DEFAULT, f, indent=2)


def load_data() -> dict:
    _ensure_data_file()
    with open(_DATA_PATH, "r") as f:
        return json.load(f)


def save_data(data: dict) -> None:
    dir_ = os.path.dirname(_DATA_PATH)
    fd, tmp = tempfile.mkstemp(dir=dir_, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, _DATA_PATH)
    except Exception:
        os.unlink(tmp)
        raise


def _next_id(data: dict, prefix: str) -> str:
    n = data.get("next_id", 1)
    data["next_id"] = n + 1
    return f"{prefix}{n}"


def add_mark(date_str: str, virtue: str, count: int) -> None:
    data = load_data()
    marks = data.setdefault("marks", {})
    day = marks.setdefault(date_str, {})
    day[virtue] = count
    save_data(data)


def get_marks(start_date: str, end_date: str) -> dict:
    data = load_data()
    result = {}
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    current = start
    while current <= end:
        ds = current.isoformat()
        if ds in data.get("marks", {}):
            result[ds] = data["marks"][ds]
        current += timedelta(days=1)
    return result


def add_todo(virtue: str, text: str) -> str:
    data = load_data()
    from datetime import datetime, timezone
    todo_id = _next_id(data, "t")
    data.setdefault("todos", []).append({
        "id": todo_id,
        "virtue": virtue,
        "text": text,
        "created": datetime.now(timezone.utc).isoformat(),
        "status": "open",
    })
    save_data(data)
    return todo_id


def set_todo_status(todo_id: str, status: str) -> None:
    if status not in {"open", "done", "cancelled"}:
        raise ValueError(f"Invalid status: {status}")
    data = load_data()
    for todo in data.get("todos", []):
        if todo["id"] == todo_id:
            todo["status"] = status
            save_data(data)
            return
    raise KeyError(f"Todo not found: {todo_id}")


def get_open_todos(virtue: str | None = None) -> list:
    data = load_data()
    todos = [t for t in data.get("todos", []) if t["status"] == "open"]
    if virtue is not None:
        todos = [t for t in todos if t["virtue"] == virtue]
    return todos


def add_note(virtue: str | None, text: str) -> str:
    data = load_data()
    from datetime import datetime, timezone
    note_id = _next_id(data, "n")
    data.setdefault("notes", []).append({
        "id": note_id,
        "virtue": virtue,
        "text": text,
        "ts": datetime.now(timezone.utc).isoformat(),
    })
    save_data(data)
    return note_id


def get_notes(virtue: str | None = None, since: str | None = None) -> list:
    data = load_data()
    notes = data.get("notes", [])
    if virtue is not None:
        notes = [n for n in notes if n.get("virtue") == virtue]
    if since is not None:
        notes = [n for n in notes if n["ts"] >= since]
    return notes


def current_focus_virtue() -> dict:
    config = load_config()
    cycle_start = date.fromisoformat(config["cycle_start"])
    today = date.today()
    weeks_elapsed = (today - cycle_start).days // 7
    idx = weeks_elapsed % len(config["virtues"])
    virtue = config["virtues"][idx]
    week_number = weeks_elapsed % len(config["virtues"]) + 1
    return {**virtue, "week_number": week_number, "weeks_elapsed": weeks_elapsed}


def record_focus_change(virtue_id: str, week_start: str) -> None:
    data = load_data()
    history = data.setdefault("focus_history", [])
    for entry in history:
        if entry["week_start"] == week_start:
            return
    history.append({"week_start": week_start, "virtue": virtue_id})
    save_data(data)


def get_owner_chat_id() -> int | None:
    data = load_data()
    return data.get("owner_chat_id")


def set_owner_chat_id(chat_id: int) -> None:
    data = load_data()
    data["owner_chat_id"] = chat_id
    save_data(data)
