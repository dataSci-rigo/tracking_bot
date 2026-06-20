import asyncio
import logging
import os
from datetime import date, timedelta

import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import store

logger = logging.getLogger(__name__)
_TZ = pytz.timezone("America/Los_Angeles")


# ---------------------------------------------------------------------------
# Job functions — also used by /debug_fire
# ---------------------------------------------------------------------------

async def job_morning():
    from bot import build_morning_message, send_message
    try:
        msg = build_morning_message()
        await send_message(msg)
    except Exception:
        logger.exception("Morning job failed")


async def job_nudge():
    from bot import build_nudge_message, send_message
    try:
        msg = build_nudge_message()
        await send_message(msg)
    except Exception:
        logger.exception("Nudge job failed")


async def job_evening():
    from bot import send_message
    try:
        await send_message("Time for your evening review. Send /web when ready.")
    except Exception:
        logger.exception("Evening job failed")


async def job_monday_rotation():
    from bot import send_message
    import inspiration
    import coach as coach_mod
    try:
        config = store.load_config()
        virtues = config["virtues"]
        current = store.current_focus_virtue()
        current_idx = next(i for i, v in enumerate(virtues) if v["id"] == current["id"])
        next_idx = (current_idx + 1) % len(virtues)
        next_virtue = virtues[next_idx]

        today = date.today()
        monday = today - timedelta(days=today.weekday())
        week_start = monday.isoformat()

        store.record_focus_change(next_virtue["id"], week_start)

        refresh_text = await asyncio.get_event_loop().run_in_executor(
            None, coach_mod.refresh_inspiration, next_virtue["id"]
        )
        if refresh_text:
            inspiration.set_weekly_refresh(next_virtue["id"], week_start, refresh_text)
            preview = refresh_text[:200]
        else:
            base = inspiration.get_morning_text(next_virtue["id"])
            preview = base[:200]

        await send_message(
            f"New week — focus is now *{next_virtue['name']}*.\n\n{preview}…"
        )
    except Exception:
        logger.exception("Monday rotation job failed")


async def job_sunday_summary():
    from bot import build_weekly_summary, send_message
    try:
        msg = build_weekly_summary()
        await send_message(msg)
    except Exception:
        logger.exception("Sunday summary job failed")


JOB_FUNCTIONS = {
    "morning": job_morning,
    "nudge": job_nudge,
    "evening": job_evening,
    "monday": job_monday_rotation,
    "sunday": job_sunday_summary,
}


# ---------------------------------------------------------------------------
# Scheduler setup
# ---------------------------------------------------------------------------

def build_scheduler() -> AsyncIOScheduler:
    config = store.load_config()
    schedule = config.get("schedule", {})

    scheduler = AsyncIOScheduler(timezone=_TZ)

    morning_time = schedule.get("morning", "07:00").split(":")
    scheduler.add_job(
        job_morning, "cron",
        hour=int(morning_time[0]), minute=int(morning_time[1]),
        id="morning",
    )

    for i, nudge_time in enumerate(schedule.get("nudges", [])):
        h, m = nudge_time.split(":")
        scheduler.add_job(
            job_nudge, "cron",
            hour=int(h), minute=int(m),
            id=f"nudge_{i}",
        )

    evening_time = schedule.get("evening", "21:00").split(":")
    scheduler.add_job(
        job_evening, "cron",
        hour=int(evening_time[0]), minute=int(evening_time[1]),
        id="evening",
    )

    scheduler.add_job(
        job_monday_rotation, "cron",
        day_of_week="mon", hour=0, minute=1,
        id="monday_rotation",
    )

    weekly_summary = schedule.get("weekly_summary", {})
    ws_day = weekly_summary.get("day", "sunday")
    ws_time = weekly_summary.get("time", "21:00").split(":")
    scheduler.add_job(
        job_sunday_summary, "cron",
        day_of_week=ws_day[:3].lower(), hour=int(ws_time[0]), minute=int(ws_time[1]),
        id="sunday_summary",
    )

    return scheduler
