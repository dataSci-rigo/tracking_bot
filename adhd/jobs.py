"""
Scheduled job callbacks. All four are called by the JobQueue.
They are also called during rehydration on startup (bot.py).
"""
import logging
from datetime import datetime, timezone, timedelta

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from . import config
from . import db

logger = logging.getLogger(__name__)


def _enabled() -> bool:
    """Per-bot on/off toggle (bot_state.py lives in todo_list/, on the path
    under run_bots.py). Standalone runs default to enabled."""
    try:
        import bot_state
        return bot_state.enabled("adhd")
    except Exception:
        return True


def _today() -> str:
    return datetime.now(config.TZ).strftime("%Y-%m-%d")


def _local_now() -> datetime:
    return datetime.now(config.TZ)


# ---------- helpers ----------

def _start_ping_keyboard(task_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Yes", callback_data=f"start_yes:{task_id}"),
        InlineKeyboardButton(f"Snooze {config.SNOOZE_MINUTES}", callback_data=f"start_snooze:{task_id}"),
        InlineKeyboardButton("Skip", callback_data=f"start_skip:{task_id}"),
    ]])


def _endpoint_keyboard(task_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Done", callback_data=f"end_done:{task_id}"),
        InlineKeyboardButton("More time", callback_data=f"end_more:{task_id}"),
        InlineKeyboardButton("Stuck", callback_data=f"end_stuck:{task_id}"),
    ]])


def _schedule_start_ping(app, task_id: int, run_at: datetime) -> None:
    now = datetime.now(timezone.utc)
    delay = max((run_at - now).total_seconds(), 0)
    app.job_queue.run_once(
        start_ping,
        when=delay,
        data=task_id,
        name=f"start_ping_{task_id}",
    )


def _schedule_endpoint_ping(app, task_id: int, run_at: datetime) -> None:
    now = datetime.now(timezone.utc)
    delay = max((run_at - now).total_seconds(), 0)
    app.job_queue.run_once(
        endpoint_ping,
        when=delay,
        data=task_id,
        name=f"endpoint_ping_{task_id}",
    )


# ---------- job: morning ----------

async def morning_prompt(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _enabled():
        return
    today = _today()
    state = db.get_day_state(today)
    if state["silenced"] or state["morning_done"]:
        return

    db.log_event("morning_prompt")
    await context.bot.send_message(
        **config.send_kwargs(),
        text=(
            "Morning. Your 1–3 must-dos today? One per line. "
            "Add a time like `Finish cover letter @ 10:00`. "
            "(`/skip` to take the day off.)"
        ),
        parse_mode="Markdown",
    )


# ---------- job: start ping ----------

async def start_ping(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _enabled():
        return
    task_id: int = context.job.data
    today = _today()

    state = db.get_day_state(today)
    if state["silenced"]:
        return

    task = db.get_task(task_id)
    if task is None or task["status"] != "planned":
        return  # already handled

    db.log_event("start_ping", task_id=task_id)
    minutes = task["timer_minutes"]
    await context.bot.send_message(
        **config.send_kwargs(),
        text=f"{task['planned_start']} — start: {task['description']}. Begin a {minutes}-min timer?",
        reply_markup=_start_ping_keyboard(task_id),
    )


# ---------- job: endpoint ping ----------

async def endpoint_ping(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _enabled():
        return
    task_id: int = context.job.data
    today = _today()

    state = db.get_day_state(today)
    if state["silenced"]:
        return

    task = db.get_task(task_id)
    if task is None or task["status"] != "started":
        return

    db.log_event("endpoint_ping", task_id=task_id)
    minutes = task["timer_minutes"]
    await context.bot.send_message(
        **config.send_kwargs(),
        text=f"{minutes} min up on: {task['description']}. Done?",
        reply_markup=_endpoint_keyboard(task_id),
    )


# ---------- job: evening ----------

async def evening_prompt(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _enabled():
        return
    today = _today()
    state = db.get_day_state(today)
    if state["silenced"] or state["evening_done"]:
        return

    db.mark_missed_tasks(today)
    tasks = db.get_tasks_for_date(today)

    if not tasks:
        lines = ["No tasks were planned today."]
    else:
        lines = ["Today:"]
        for t in tasks:
            if t["status"] == "done":
                lines.append(f"✓ {t['description']}")
            elif t["status"] in ("missed", "started"):
                lines.append(f"✗ {t['description']} (not started)")
            elif t["status"] == "skipped":
                lines.append(f"– {t['description']} (skipped)")
            elif t["status"] == "stuck":
                lines.append(f"– {t['description']} (stuck)")
            else:
                lines.append(f"– {t['description']} (unscheduled)")

    lines.append("\nOne line — what got in the way?")
    db.log_event("evening_prompt")
    db.set_day_flag(today, "evening_prompted", 1)
    await context.bot.send_message(
        **config.send_kwargs(),
        text="\n".join(lines),
    )


# ---------- daily scheduling (self-rescheduling run_once) ----------
#
# run_daily(tzinfo=ZoneInfo(...)) is unreliable in APScheduler 3.x — it may
# fire at 7 AM UTC instead of 7 AM local time. These helpers compute the exact
# next UTC instant and use run_once, then reschedule themselves after each fire.

def _next_occurrence_utc(hour: int, minute: int) -> datetime:
    """Next future UTC datetime for the given local clock time (today or tomorrow)."""
    now_local = _local_now()
    target = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now_local:
        target += timedelta(days=1)
    return target.astimezone(timezone.utc)


async def _morning_wrapper(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await morning_prompt(context)
    finally:
        # Always reschedule tomorrow, even if today's send/DB call failed —
        # otherwise one failure silently kills every future morning prompt.
        mh, mm = config.morning_time()
        _run_once_at(context.application, _morning_wrapper, _next_occurrence_utc(mh, mm), "morning_scheduled")


async def _evening_wrapper(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await evening_prompt(context)
    finally:
        eh, em = config.evening_time()
        _run_once_at(context.application, _evening_wrapper, _next_occurrence_utc(eh, em), "evening_scheduled")


def _run_once_at(app, callback, run_at_utc: datetime, name: str) -> None:
    delay = max((run_at_utc - datetime.now(timezone.utc)).total_seconds(), 0)
    app.job_queue.run_once(callback, when=delay, name=name)


def schedule_morning(app) -> None:
    mh, mm = config.morning_time()
    run_at = _next_occurrence_utc(mh, mm)
    logger.info("Morning prompt scheduled for %s UTC", run_at.strftime("%Y-%m-%d %H:%M"))
    _run_once_at(app, _morning_wrapper, run_at, "morning_scheduled")


def schedule_evening(app) -> None:
    eh, em = config.evening_time()
    run_at = _next_occurrence_utc(eh, em)
    logger.info("Evening prompt scheduled for %s UTC", run_at.strftime("%Y-%m-%d %H:%M"))
    _run_once_at(app, _evening_wrapper, run_at, "evening_scheduled")


# ---------- rehydrate (called from bot.py on startup) ----------

async def _send_rehydrate_alert(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _enabled():
        return
    if config.CHAT_ID:
        await context.bot.send_message(**config.send_kwargs(), text=context.job.data)


def rehydrate_jobs(app) -> None:
    """Re-register today's pending jobs from DB after a restart."""
    today = _today()
    now_utc = datetime.now(timezone.utc)
    now_local = _local_now()

    # If the bot restarted after the morning ping time, run_daily won't fire
    # until tomorrow. Send the morning prompt now if we missed it today.
    state = db.get_day_state(today)
    if not state["morning_done"] and not state["silenced"]:
        mh, mm = config.morning_time()
        morning_dt = now_local.replace(hour=mh, minute=mm, second=0, microsecond=0)
        noon_dt    = now_local.replace(hour=12, minute=0, second=0, microsecond=0)
        if morning_dt <= now_local <= noon_dt:
            logger.info("Morning prompt missed (bot restarted after %02d:%02d) — firing now", mh, mm)
            app.job_queue.run_once(morning_prompt, when=5, name="morning_rehydrate")

    rehydrated = 0
    missed: list[str] = []

    tasks = db.get_tasks_for_date(today)
    for task in tasks:
        task_id = task["id"]

        if task["status"] == "planned" and task["planned_start"]:
            h, m = task["planned_start"].split(":")
            run_at_local = now_local.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
            run_at_utc = run_at_local.astimezone(timezone.utc)
            if run_at_utc > now_utc:
                _schedule_start_ping(app, task_id, run_at_utc)
                rehydrated += 1
                logger.info("Rehydrated start_ping for task %d at %s", task_id, task["planned_start"])
            else:
                # Bot was down through this task's start time — no ping will
                # ever fire for it now unless someone notices and /at's it.
                missed.append(f"start ping for \"{task['description']}\" ({task['planned_start']})")

        elif task["status"] == "started" and task["started_at"]:
            started = datetime.fromisoformat(task["started_at"])
            run_at_utc = started + timedelta(minutes=task["timer_minutes"])
            _schedule_endpoint_ping(app, task_id, run_at_utc)
            rehydrated += 1
            logger.info("Rehydrated endpoint_ping for task %d (fires at %s)", task_id, run_at_utc)

    logger.info(
        "Rehydration complete: %d job(s) restored, %d missed window(s)",
        rehydrated, len(missed),
    )
    if missed:
        text = "⚠️ Restarted and missed:\n" + "\n".join(missed)
        app.job_queue.run_once(_send_rehydrate_alert, when=5, data=text, name="rehydrate_alert")


# ---------- daily-job self-heal (defense in depth vs. the self-rescheduling
# morning/evening jobs silently vanishing, e.g. if a future bug reintroduces
# the failure-breaks-the-chain problem) ----------

async def check_daily_jobs_scheduled(context: ContextTypes.DEFAULT_TYPE) -> None:
    app = context.application
    names = {job.name for job in app.job_queue.jobs()}
    recovered = []

    if "morning_scheduled" not in names:
        schedule_morning(app)
        recovered.append("morning")
    if "evening_scheduled" not in names:
        schedule_evening(app)
        recovered.append("evening")

    if recovered:
        logger.error("Daily job(s) missing, rescheduled: %s", recovered)
        if config.CHAT_ID:
            await context.bot.send_message(
                **config.send_kwargs(),
                text=f"⚠️ Recovered missing daily job(s): {', '.join(recovered)}. Rescheduled for the next occurrence.",
            )
