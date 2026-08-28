import aiosqlite
from datetime import date, datetime
from typing import Optional
from .config import DB_PATH

_DDL = """
CREATE TABLE IF NOT EXISTS cycles (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL,
    started_at      TEXT    NOT NULL,
    challenge_type  TEXT    NOT NULL,   -- i_will | i_wont | i_want
    challenge_text  TEXT    NOT NULL,
    i_want_anchor   TEXT    NOT NULL,
    current_week    INTEGER DEFAULT 1,
    status          TEXT    DEFAULT 'active',  -- active | archived
    created_at      TEXT    DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS daily_entries (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id             INTEGER NOT NULL,
    cycle_id            INTEGER NOT NULL,
    date                TEXT    NOT NULL,  -- YYYY-MM-DD
    week_number         INTEGER NOT NULL,
    energy_level        INTEGER,           -- 1-5
    challenge_adherence TEXT,              -- yes | no | partial
    urge_count          INTEGER DEFAULT 0,
    microscope_obs      TEXT,
    reflection_text     TEXT,
    created_at          TEXT    DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(cycle_id, date)
);

CREATE TABLE IF NOT EXISTS urges (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL,
    cycle_id      INTEGER NOT NULL,
    challenge_id  INTEGER,             -- FK to challenges.id, NULL = primary challenge
    timestamp     TEXT    NOT NULL,
    trigger_text  TEXT,
    gave_in       INTEGER,             -- 0 | 1
    intensity     INTEGER,             -- 1-5
    notes         TEXT
);

CREATE TABLE IF NOT EXISTS boosters (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id            INTEGER NOT NULL,
    cycle_id           INTEGER NOT NULL,
    date               TEXT    NOT NULL,  -- YYYY-MM-DD
    sleep_hours        REAL,
    exercise_done      INTEGER DEFAULT 0,
    meditation_minutes INTEGER DEFAULT 0,
    breathing_done     INTEGER DEFAULT 0,
    UNIQUE(cycle_id, date)
);

CREATE TABLE IF NOT EXISTS challenges (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL,
    cycle_id       INTEGER NOT NULL,
    challenge_type TEXT    NOT NULL,   -- i_will | i_wont | i_want
    challenge_text TEXT    NOT NULL,
    sort_order     INTEGER DEFAULT 0,
    created_at     TEXT    DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS weekly_syntheses (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id              INTEGER NOT NULL,
    cycle_id             INTEGER NOT NULL,
    week_number          INTEGER NOT NULL,
    generated_at         TEXT    NOT NULL,
    claude_response_text TEXT,
    input_summary        TEXT,
    UNIQUE(cycle_id, week_number)
);

CREATE TABLE IF NOT EXISTS challenge_adherence_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    cycle_id     INTEGER NOT NULL,
    date         TEXT    NOT NULL,
    challenge_id INTEGER NOT NULL,
    adherence    TEXT    NOT NULL,   -- yes | partial | no
    UNIQUE(cycle_id, date, challenge_id)
);
"""


async def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(_DDL)
        for sql in [
            "ALTER TABLE urges ADD COLUMN challenge_id INTEGER",
            "ALTER TABLE urges ADD COLUMN outcome TEXT",
            "ALTER TABLE urges ADD COLUMN skill_week INTEGER",
            "ALTER TABLE urges ADD COLUMN i_want_recalled INTEGER",
            "ALTER TABLE urges ADD COLUMN breathing_done INTEGER",
            "ALTER TABLE urges ADD COLUMN recovery_action TEXT",
            "ALTER TABLE daily_entries ADD COLUMN morning_energy INTEGER",
        ]:
            try:
                await db.execute(sql)
            except Exception:
                pass  # column already exists
        await db.commit()


# ── cycles ────────────────────────────────────────────────────────────────────

async def create_cycle(user_id: int, challenge_type: str, challenge_text: str,
                       i_want_anchor: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO cycles (user_id, started_at, challenge_type, challenge_text, i_want_anchor) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, date.today().isoformat(), challenge_type, challenge_text, i_want_anchor),
        )
        cycle_id = cur.lastrowid
        # Mirror initial challenge into challenges table so all code uses one source
        await db.execute(
            "INSERT INTO challenges (user_id, cycle_id, challenge_type, challenge_text, sort_order) "
            "VALUES (?, ?, ?, ?, 0)",
            (user_id, cycle_id, challenge_type, challenge_text),
        )
        await db.commit()
        return cycle_id


async def get_active_cycles(user_id: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM cycles WHERE user_id=? AND status='active' ORDER BY id DESC",
            (user_id,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_active_cycle(user_id: int) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM cycles WHERE user_id=? AND status='active' ORDER BY id DESC LIMIT 1",
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def archive_cycle(cycle_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE cycles SET status='archived' WHERE id=?", (cycle_id,))
        await db.commit()


async def advance_week(cycle_id: int, new_week: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE cycles SET current_week=? WHERE id=?", (new_week, cycle_id))
        await db.commit()


# ── daily entries ─────────────────────────────────────────────────────────────

async def upsert_daily_entry(user_id: int, cycle_id: int, week_number: int,
                              **fields) -> None:
    today = date.today().isoformat()
    cols = ", ".join(fields.keys())
    placeholders = ", ".join("?" * len(fields))
    updates = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values())
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            f"INSERT INTO daily_entries (user_id, cycle_id, date, week_number, {cols}) "
            f"VALUES (?, ?, ?, ?, {placeholders}) "
            f"ON CONFLICT(cycle_id, date) DO UPDATE SET {updates}",
            [user_id, cycle_id, today, week_number, *vals, *vals],
        )
        await db.commit()


async def get_daily_entry(cycle_id: int, for_date: Optional[str] = None) -> Optional[dict]:
    target = for_date or date.today().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM daily_entries WHERE cycle_id=? AND date=?",
            (cycle_id, target),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_week_entries(cycle_id: int, week_number: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM daily_entries WHERE cycle_id=? AND week_number=? ORDER BY date",
            (cycle_id, week_number),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


# ── urges ─────────────────────────────────────────────────────────────────────

async def log_urge(user_id: int, cycle_id: int, trigger_text: Optional[str],
                   gave_in: Optional[bool], intensity: Optional[int],
                   notes: Optional[str] = None,
                   challenge_id: Optional[int] = None,
                   outcome: Optional[str] = None,
                   skill_week: Optional[int] = None,
                   i_want_recalled: Optional[int] = None,
                   breathing_done: Optional[int] = None,
                   recovery_action: Optional[str] = None) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO urges "
            "(user_id, cycle_id, challenge_id, timestamp, trigger_text, gave_in, intensity, notes, "
            "outcome, skill_week, i_want_recalled, breathing_done, recovery_action) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, cycle_id, challenge_id, datetime.now().isoformat(),
             trigger_text, int(gave_in) if gave_in is not None else None,
             intensity, notes,
             outcome, skill_week, i_want_recalled, breathing_done, recovery_action),
        )
        await db.commit()
        return cur.lastrowid


async def get_today_urges(cycle_id: int) -> list[dict]:
    today = date.today().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM urges WHERE cycle_id=? AND date(timestamp)=? ORDER BY timestamp",
            (cycle_id, today),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_week_urges(cycle_id: int, week_number: int,
                         started_at: str) -> list[dict]:
    # Get urges for days that fall within the given week (relative to started_at)
    from datetime import timedelta
    start = date.fromisoformat(started_at) + timedelta(weeks=week_number - 1)
    end = start + timedelta(days=6)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM urges WHERE cycle_id=? AND date(timestamp) BETWEEN ? AND ? ORDER BY timestamp",
            (cycle_id, start.isoformat(), end.isoformat()),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


# ── boosters ──────────────────────────────────────────────────────────────────

async def upsert_boosters(user_id: int, cycle_id: int, **fields) -> None:
    today = date.today().isoformat()
    cols = ", ".join(fields.keys())
    placeholders = ", ".join("?" * len(fields))
    updates = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values())
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            f"INSERT INTO boosters (user_id, cycle_id, date, {cols}) "
            f"VALUES (?, ?, ?, {placeholders}) "
            f"ON CONFLICT(cycle_id, date) DO UPDATE SET {updates}",
            [user_id, cycle_id, today, *vals, *vals],
        )
        await db.commit()


async def get_today_boosters(cycle_id: int) -> Optional[dict]:
    today = date.today().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM boosters WHERE cycle_id=? AND date=?",
            (cycle_id, today),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_week_boosters(cycle_id: int, week_number: int,
                             started_at: str) -> list[dict]:
    from datetime import timedelta
    start = date.fromisoformat(started_at) + timedelta(weeks=week_number - 1)
    end = start + timedelta(days=6)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM boosters WHERE cycle_id=? AND date BETWEEN ? AND ?",
            (cycle_id, start.isoformat(), end.isoformat()),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


# ── weekly syntheses ──────────────────────────────────────────────────────────

async def save_synthesis(user_id: int, cycle_id: int, week_number: int,
                          response_text: str, input_summary: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO weekly_syntheses "
            "(user_id, cycle_id, week_number, generated_at, claude_response_text, input_summary) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(cycle_id, week_number) DO UPDATE SET "
            "generated_at=excluded.generated_at, "
            "claude_response_text=excluded.claude_response_text, "
            "input_summary=excluded.input_summary",
            (user_id, cycle_id, week_number, datetime.now().isoformat(),
             response_text, input_summary),
        )
        await db.commit()


# ── challenges ────────────────────────────────────────────────────────────────

async def get_challenges(cycle_id: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM challenges WHERE cycle_id=? ORDER BY sort_order, id",
            (cycle_id,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def add_challenge(user_id: int, cycle_id: int, challenge_type: str,
                        challenge_text: str, sort_order: int = 0) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO challenges (user_id, cycle_id, challenge_type, challenge_text, sort_order) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, cycle_id, challenge_type, challenge_text, sort_order),
        )
        await db.commit()
        return cur.lastrowid


async def update_challenge(challenge_id: int, user_id: int,
                           challenge_type: str, challenge_text: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE challenges SET challenge_type=?, challenge_text=? "
            "WHERE id=? AND user_id=?",
            (challenge_type, challenge_text, challenge_id, user_id),
        )
        await db.commit()


async def delete_challenge(challenge_id: int, user_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM challenges WHERE id=? AND user_id=?",
            (challenge_id, user_id),
        )
        await db.commit()


async def get_synthesis(cycle_id: int, week_number: int) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM weekly_syntheses WHERE cycle_id=? AND week_number=?",
            (cycle_id, week_number),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


# ── challenge adherence log ───────────────────────────────────────────────────

async def log_challenge_adherence(user_id: int, cycle_id: int,
                                   challenge_id: int, adherence: str) -> None:
    today = date.today().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO challenge_adherence_log (user_id, cycle_id, date, challenge_id, adherence) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(cycle_id, date, challenge_id) DO UPDATE SET adherence=excluded.adherence",
            (user_id, cycle_id, today, challenge_id, adherence),
        )
        await db.commit()


async def get_challenge_adherences(cycle_id: int,
                                    for_date: Optional[str] = None) -> list[dict]:
    target = for_date or date.today().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT cal.*, c.challenge_text, c.challenge_type "
            "FROM challenge_adherence_log cal "
            "JOIN challenges c ON c.id = cal.challenge_id "
            "WHERE cal.cycle_id=? AND cal.date=? ORDER BY c.sort_order, c.id",
            (cycle_id, target),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_challenge_adherences_all(cycle_id: int,
                                        challenge_id: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM challenge_adherence_log "
            "WHERE cycle_id=? AND challenge_id=? ORDER BY date",
            (cycle_id, challenge_id),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_challenge_urges(cycle_id: int, challenge_id: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM urges WHERE cycle_id=? AND challenge_id=? ORDER BY timestamp",
            (cycle_id, challenge_id),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]
