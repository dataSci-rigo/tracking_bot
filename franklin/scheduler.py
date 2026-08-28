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


def _enabled() -> bool:
    """Per-bot on/off toggle (bot_state.py in todo_list/, on the path under
    run_bots.py). Standalone runs default to enabled."""
    try:
        import bot_state
        return bot_state.enabled("franklin")
    except Exception:
        return True


async def job_morning():
    from bot import build_morning_message, send_message, is_paused
    if is_paused() or not _enabled():
        return
    try:
        msg = build_morning_message()
        await send_message(msg)
    except Exception:
        logger.exception("Morning job failed")


async def job_nudge():
    from bot import build_nudge_message, send_message, is_paused
    import advice_store
    if is_paused() or not _enabled():
        return
    try:
        today = date.today().isoformat()
        item = advice_store.take_next_unsent(today)
        msg = item["text"] if item is not None else build_nudge_message()
        message_id = await send_message(msg)
        if item is not None and message_id is not None:
            advice_store.mark_sent(today, item["idx"], message_id)
    except Exception:
        logger.exception("Nudge job failed")


async def job_generate_advice():
    from bot import is_paused
    import advice_store
    import coach as coach_mod
    if is_paused() or not _enabled():
        return
    try:
        virtue = store.current_focus_virtue()
        today = date.today().isoformat()
        items = await asyncio.get_event_loop().run_in_executor(
            None, coach_mod.generate_daily_advice, virtue["id"]
        )
        advice_store.save_daily_advice(today, virtue["id"], virtue["name"], items)
        logger.info("Generated %d advice item(s) for %s", len(items), virtue["name"])
    except Exception:
        logger.exception("Advice generation job failed")


async def job_evening():
    from bot import send_message
    if not _enabled():
        return
    try:
        await send_message("Time for your evening review. Send /web when ready.")
    except Exception:
        logger.exception("Evening job failed")


async def job_monday_rotation():
    """Announce the new week's virtue. current_focus_virtue() is purely
    date-driven ((today - cycle_start).days // 7 % len(virtues)) and already
    auto-advances the instant the calendar rolls into Monday — this job
    fires at 00:01 Monday, by which point date.today() is already that new
    Monday, so store.current_focus_virtue() already IS the new week's
    virtue. Do not increment it again here — a prior version did, which
    silently skipped a virtue every single week (announced one virtue ahead
    of what /focus, /today, and every other command showed all week)."""
    from bot import send_message
    import inspiration
    import coach as coach_mod
    if not _enabled():
        return
    try:
        virtue = store.current_focus_virtue()

        today = date.today()
        monday = today - timedelta(days=today.weekday())
        week_start = monday.isoformat()

        store.record_focus_change(virtue["id"], week_start)

        refresh_text = await asyncio.get_event_loop().run_in_executor(
            None, coach_mod.refresh_inspiration, virtue["id"]
        )
        if refresh_text:
            inspiration.set_weekly_refresh(virtue["id"], week_start, refresh_text)
            preview = refresh_text[:200]
        else:
            base = inspiration.get_morning_text(virtue["id"])
            preview = base[:200]

        await send_message(
            f"New week — focus is now *{virtue['name']}*.\n\n{preview}…"
        )
    except Exception:
        logger.exception("Monday rotation job failed")


async def job_sunday_summary():
    from bot import build_weekly_summary, send_message
    if not _enabled():
        return
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
    "generate_advice": job_generate_advice,
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

    advice_gen_time = schedule.get("generate_advice", "06:30").split(":")
    scheduler.add_job(
        job_generate_advice, "cron",
        hour=int(advice_gen_time[0]), minute=int(advice_gen_time[1]),
        id="generate_advice",
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
