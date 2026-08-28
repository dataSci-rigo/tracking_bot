from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from .config import DB_PATH


@contextmanager
def _conn():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def init_db() -> None:
    import os
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with _conn() as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                date                  TEXT    NOT NULL,
                description           TEXT    NOT NULL,
                planned_start         TEXT,
                timer_minutes         INTEGER NOT NULL,
                status                TEXT    NOT NULL DEFAULT 'planned',
                created_at            TEXT    NOT NULL,
                started_at            TEXT,
                completed_at          TEXT,
                outcome_note          TEXT,
                ai_estimate_minutes   INTEGER,
                user_estimate_minutes INTEGER,
                actual_minutes        INTEGER
            );

            CREATE TABLE IF NOT EXISTS events (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                ts         TEXT    NOT NULL,
                event_type TEXT    NOT NULL,
                task_id    INTEGER,
                payload    TEXT
            );

            CREATE TABLE IF NOT EXISTS day_state (
                date             TEXT PRIMARY KEY,
                silenced         INTEGER NOT NULL DEFAULT 0,
                morning_done     INTEGER NOT NULL DEFAULT 0,
                evening_done     INTEGER NOT NULL DEFAULT 0,
                evening_prompted INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS lessons (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                date        TEXT    NOT NULL,
                went_well   TEXT,
                to_improve  TEXT,
                learning    TEXT,
                created_at  TEXT    NOT NULL
            );
        """)
        # Migrate existing DBs that predate estimate columns
        existing = {row[1] for row in con.execute("PRAGMA table_info(tasks)").fetchall()}
        for col, typedef in [
            ("ai_estimate_minutes",   "INTEGER"),
            ("user_estimate_minutes", "INTEGER"),
            ("actual_minutes",        "INTEGER"),
        ]:
            if col not in existing:
                con.execute(f"ALTER TABLE tasks ADD COLUMN {col} {typedef}")

        # Migrate existing DBs that predate evening_prompted
        existing_day_state = {row[1] for row in con.execute("PRAGMA table_info(day_state)").fetchall()}
        if "evening_prompted" not in existing_day_state:
            con.execute("ALTER TABLE day_state ADD COLUMN evening_prompted INTEGER NOT NULL DEFAULT 0")


# ---------- day_state helpers ----------

def get_day_state(date: str) -> sqlite3.Row:
    with _conn() as con:
        row = con.execute("SELECT * FROM day_state WHERE date = ?", (date,)).fetchone()
        if row is None:
            con.execute(
                "INSERT INTO day_state (date) VALUES (?)",
                (date,),
            )
        row = con.execute("SELECT * FROM day_state WHERE date = ?", (date,)).fetchone()
    return row


def set_day_flag(date: str, column: str, value: int) -> None:
    allowed = {"silenced", "morning_done", "evening_done", "evening_prompted"}
    if column not in allowed:
        raise ValueError(f"Unknown column: {column}")
    with _conn() as con:
        con.execute(f"""
            INSERT INTO day_state (date, {column}) VALUES (?, ?)
            ON CONFLICT(date) DO UPDATE SET {column} = excluded.{column}
        """, (date, value))


# ---------- tasks ----------

def add_task(date: str, description: str, planned_start: str | None, timer_minutes: int) -> int:
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as con:
        cur = con.execute(
            """INSERT INTO tasks (date, description, planned_start, timer_minutes, status, created_at)
               VALUES (?, ?, ?, ?, 'planned', ?)""",
            (date, description, planned_start, timer_minutes, now),
        )
        return cur.lastrowid


def get_tasks_for_date(date: str) -> list[sqlite3.Row]:
    with _conn() as con:
        return con.execute(
            "SELECT * FROM tasks WHERE date = ? ORDER BY id",
            (date,),
        ).fetchall()


def get_tasks_for_range(start_date: str, end_date: str) -> list[sqlite3.Row]:
    with _conn() as con:
        return con.execute(
            "SELECT * FROM tasks WHERE date BETWEEN ? AND ? ORDER BY date, id",
            (start_date, end_date),
        ).fetchall()


def get_tasks_with_actuals(limit: int = 200) -> list[sqlite3.Row]:
    """Tasks that have a logged actual_minutes, most recent first."""
    with _conn() as con:
        return con.execute(
            """SELECT * FROM tasks WHERE actual_minutes IS NOT NULL
               ORDER BY date DESC, id DESC LIMIT ?""",
            (limit,),
        ).fetchall()


def get_task(task_id: int) -> sqlite3.Row | None:
    with _conn() as con:
        return con.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()


def update_task_status(task_id: int, status: str, **kwargs) -> None:
    allowed_fields = {"started_at", "completed_at", "outcome_note"}
    sets = ["status = ?"]
    vals: list = [status]
    for k, v in kwargs.items():
        if k not in allowed_fields:
            raise ValueError(f"Unknown field: {k}")
        sets.append(f"{k} = ?")
        vals.append(v)
    vals.append(task_id)
    with _conn() as con:
        con.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", vals)


def update_timer_minutes(task_id: int, minutes: int) -> None:
    with _conn() as con:
        con.execute("UPDATE tasks SET timer_minutes = ? WHERE id = ?", (minutes, task_id))


def update_task_time_estimates(
    task_id: int,
    *,
    ai_estimate: int | None = None,
    user_estimate: int | None = None,
    actual: int | None = None,
) -> None:
    sets, vals = [], []
    if ai_estimate is not None:
        sets.append("ai_estimate_minutes = ?"); vals.append(ai_estimate)
    if user_estimate is not None:
        sets.append("user_estimate_minutes = ?"); vals.append(user_estimate)
    if actual is not None:
        sets.append("actual_minutes = ?"); vals.append(actual)
    if not sets:
        return
    vals.append(task_id)
    with _conn() as con:
        con.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", vals)


def mark_missed_tasks(date: str) -> None:
    with _conn() as con:
        con.execute(
            "UPDATE tasks SET status = 'missed' WHERE date = ? AND status IN ('planned', 'started', 'paused')",
            (date,),
        )


# ---------- events ----------

def log_event(event_type: str, task_id: int | None = None, payload: str | None = None) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as con:
        con.execute(
            "INSERT INTO events (ts, event_type, task_id, payload) VALUES (?, ?, ?, ?)",
            (now, event_type, task_id, payload),
        )


# ---------- lessons ----------

def add_lesson(date: str, went_well: str, to_improve: str, learning: str | None) -> int:
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as con:
        cur = con.execute(
            "INSERT INTO lessons (date, went_well, to_improve, learning, created_at) VALUES (?, ?, ?, ?, ?)",
            (date, went_well, to_improve, learning, now),
        )
        return cur.lastrowid


def get_lessons(limit: int = 10) -> list[sqlite3.Row]:
    with _conn() as con:
        return con.execute(
            "SELECT * FROM lessons ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()


def get_lessons_for_range(start_date: str, end_date: str) -> list[sqlite3.Row]:
    with _conn() as con:
        return con.execute(
            "SELECT * FROM lessons WHERE date BETWEEN ? AND ? ORDER BY date",
            (start_date, end_date),
        ).fetchall()
