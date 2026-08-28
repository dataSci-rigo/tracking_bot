"""
Telegram handlers: commands, morning/evening free text, inline callback queries.
"""
from __future__ import annotations
import asyncio
import logging
import re
from collections import Counter
from datetime import datetime, timezone, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from . import ai as ai_mod
from . import config
from . import db
from .jobs import (
    _schedule_start_ping,
    _schedule_endpoint_ping,
    _start_ping_keyboard,
    _endpoint_keyboard,
)

logger = logging.getLogger(__name__)


def _today() -> str:
    return datetime.now(config.TZ).strftime("%Y-%m-%d")


def _local_now() -> datetime:
    return datetime.now(config.TZ)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ---------- pending replies (reply-required, 3h timeout) ----------
#
# Bot prompts (estimate/actual-time/outcome/lesson questions) are only
# fulfilled by an explicit Telegram reply to that exact prompt message —
# never by "whatever text arrives next" — and expire after PENDING_REPLY_TIMEOUT.
# This also lets several prompts stay open at once (e.g. finishing two
# started tasks back to back each opens its own "actual time?" question).

PENDING_REPLY_TIMEOUT = timedelta(hours=3)


def _is_fresh(entry: dict) -> bool:
    return _utc_now() - datetime.fromisoformat(entry["ts"]) <= PENDING_REPLY_TIMEOUT


def _prune_pending(context: ContextTypes.DEFAULT_TYPE, key: str) -> list:
    pending = context.user_data.get(key, [])
    fresh = [p for p in pending if _is_fresh(p)]
    context.user_data[key] = fresh
    return fresh


def _add_pending(context: ContextTypes.DEFAULT_TYPE, key: str, msg_id: int, **data) -> None:
    pending = context.user_data.setdefault(key, [])
    pending.append({"msg_id": msg_id, "ts": _utc_now().isoformat(), **data})


def _pop_pending_by_reply(context: ContextTypes.DEFAULT_TYPE, key: str, reply_to_id: int | None) -> dict | None:
    if reply_to_id is None:
        return None
    pending = _prune_pending(context, key)
    for i, p in enumerate(pending):
        if p["msg_id"] == reply_to_id:
            return pending.pop(i)
    return None


def _pop_latest_pending(context: ContextTypes.DEFAULT_TYPE, key: str) -> dict | None:
    pending = _prune_pending(context, key)
    return pending.pop() if pending else None


# ---------- task-status groupings ----------
# "started" (currently running) and "done" are the only statuses /begin and
# its buttons refuse to touch — planned, stuck, paused, missed, and skipped
# tasks can all be (re)started.
_RUNNING_OR_DONE = ("started", "done")


# ---------- auth guard ----------

def _authorized(update: Update) -> bool:
    if config.CHAT_ID is None:
        return True
    if update.effective_chat.id != config.CHAT_ID:
        return False
    if config.THREAD_ID:
        thread = getattr(update.effective_message, "message_thread_id", None)
        return thread == config.THREAD_ID
    return True


# ---------- time parsing ----------

_TIME_PATTERNS = [
    re.compile(r"@\s*(\d{1,2}):(\d{2})\s*(am|pm)\s*$", re.I),   # @ 10:30 AM/PM
    re.compile(r"\bat\s+(\d{1,2}):(\d{2})\s*(am|pm)\s*$", re.I), # at 10:30 AM/PM
    re.compile(r"@\s*(\d{1,2}):(\d{2})\s*$"),                     # @ 10:30 (24h)
    re.compile(r"\bat\s+(\d{1,2}):(\d{2})\s*$", re.I),            # at 10:30 (24h)
    re.compile(r"@\s*(\d{1,2})\s*(am|pm)\s*$", re.I),             # @ 2pm
    re.compile(r"\bat\s+(\d{1,2})\s*(am|pm)\s*$", re.I),          # at 2pm
    re.compile(r"@\s*(\d{1,2})\s*$"),                              # @ 10 (24h)
    re.compile(r"\bat\s+(\d{1,2})\s*$", re.I),                     # at 10 (24h)
]

_NUMERIC_PREFIX = re.compile(r"^\d+[\.\)]\s*")


def _parse_task_line(line: str) -> tuple[str, str | None]:
    """Return (description, planned_start_HH:MM or None)."""
    line = line.strip()
    for pat in _TIME_PATTERNS:
        m = pat.search(line)
        if m:
            desc = line[:m.start()].strip().rstrip("@").strip()
            desc = _NUMERIC_PREFIX.sub("", desc)  # strip leading "1. " or "3) "
            groups = m.groups()
            # groups is (hour, minute, ampm) or (hour, ampm) or (hour,)
            if len(groups) == 3:
                # HH:MM AM/PM
                hour, minute, ampm = int(groups[0]), groups[1], groups[2].lower()
                if ampm == "pm" and hour != 12:
                    hour += 12
                if ampm == "am" and hour == 12:
                    hour = 0
                return desc, f"{hour:02d}:{minute}"
            elif len(groups) == 2 and groups[1].lower() in ("am", "pm"):
                # H AM/PM
                hour, ampm = int(groups[0]), groups[1].lower()
                if ampm == "pm" and hour != 12:
                    hour += 12
                if ampm == "am" and hour == 12:
                    hour = 0
                return desc, f"{hour:02d}:00"
            elif len(groups) == 2:
                # HH:MM (24h)
                return desc, f"{int(groups[0]):02d}:{groups[1]}"
            else:
                # H (24h)
                return desc, f"{int(groups[0]):02d}:00"
    desc = _NUMERIC_PREFIX.sub("", line)
    return desc, None


def _parse_time_only(text: str) -> str | None:
    """Parse a bare time string like '8:30 PM' or '@9pm'. Returns 'HH:MM' or None."""
    _, start = _parse_task_line("x " + text)
    return start


_HELP_TEXT = """\
🧠 *ADHD Bot*

*Daily flow*
Morning ping → send your task list, one per line:
  `Call dentist @ 14:00`
  `Finish report`
  `Gym @ 18:00`
Reply *done* when your plan is set.
Evening ping → tell me how it went.

*Add a task anytime*
/todo Buy milk
/todo Submit report @ 3pm

*Commands*
/today — see today's task list
/todo <task> — add a task to today
/begin — start or resume any task that isn't currently running or done (with or without a time)
/pause\_task — pause a running timer without marking the task done
/skip — skip today's morning plan
/snooze — snooze the current reminder
/silence\_today — no more pings today
/done — finish the running task (or lock in your morning plan)
/complete\_task — pick any open task, including ones you never started, to mark done
/lesson — log a lesson learned
/lessons — view past lessons
/weekly — weekly review: done/missed/stuck + lessons
/trends — estimate vs. actual time trends
/help — show this message
"""


# ---------- /start ----------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    db.log_event("command", payload="/start")
    await update.message.reply_text(
        f"Hey. Your chat ID is `{chat_id}`.\n\n"
        "Paste that into `TELEGRAM_CHAT_ID` in your `.env`, then restart the bot.\n\n"
        + _HELP_TEXT,
        parse_mode="Markdown",
    )


# ---------- /help ----------

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    db.log_event("command", payload="/help")
    await update.message.reply_text(_HELP_TEXT, parse_mode="Markdown")


# ---------- /today ----------

async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    today = _today()
    tasks = db.get_tasks_for_date(today)
    db.log_event("command", payload="/today")

    if not tasks:
        await update.message.reply_text("No tasks planned yet today. I'll ping you at morning time, or just send me your list now.")
        return

    status_icon = {
        "planned": "○",
        "started": "▶",
        "paused": "⏸",
        "done": "✓",
        "skipped": "–",
        "stuck": "?",
        "missed": "✗",
    }
    lines = ["Today's plan:"]
    for t in tasks:
        icon = status_icon.get(t["status"], "○")
        time_str = f"  {t['planned_start']}" if t["planned_start"] else ""
        lines.append(f"{icon} {t['description']}{time_str}  [{t['status']}]")
    await update.message.reply_text("\n".join(lines))


# ---------- /skip ----------

async def cmd_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    # If bot is mid-conversation, /skip means "skip this prompt", not "skip the day".
    # Reply to a specific prompt to target it; otherwise the most recently
    # opened pending prompt is skipped.
    reply_to_id = (update.message.reply_to_message.message_id
                   if update.message.reply_to_message else None)

    lesson_pending = context.user_data.get("lesson_pending")
    if lesson_pending and _is_fresh(lesson_pending) and (reply_to_id is None or reply_to_id == lesson_pending["msg_id"]):
        await handle_lesson_response(update, context)
        return

    for key, handler in (
        ("pending_estimates", _handle_user_estimate),
        ("pending_actuals", _handle_actual_time),
        ("pending_outcomes", handle_outcome_note),
    ):
        entry = (_pop_pending_by_reply(context, key, reply_to_id) if reply_to_id
                 else _pop_latest_pending(context, key))
        if entry:
            await handler(update, context, entry["task_id"])
            return

    today = _today()
    db.set_day_flag(today, "morning_done", 1)
    db.log_event("silence_today", payload="/skip")
    await update.message.reply_text("Got it — taking the day off. No tasks logged.")


# ---------- /at — reschedule task by number ----------

async def cmd_at(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    args_text = " ".join(context.args).strip() if context.args else ""
    parts = args_text.split(None, 1)
    if len(parts) < 2:
        await update.message.reply_text(
            "Usage: `/at 1 8:30 PM` — sets the time on task #1 and schedules its reminder.",
            parse_mode="Markdown",
        )
        return
    try:
        task_num = int(parts[0])
    except ValueError:
        await update.message.reply_text("First argument must be a task number. `/at 1 8:30 PM`", parse_mode="Markdown")
        return

    planned_start = _parse_time_only(parts[1])
    if not planned_start:
        await update.message.reply_text(f"Couldn't parse time `{parts[1]}`. Try `8:30 PM` or `20:30`.", parse_mode="Markdown")
        return

    today = _today()
    tasks = db.get_tasks_for_date(today)
    if task_num < 1 or task_num > len(tasks):
        await update.message.reply_text(f"No task #{task_num}. You have {len(tasks)} task(s) today.")
        return

    task = tasks[task_num - 1]
    task_id = task["id"]
    if task["status"] not in ("planned",):
        await update.message.reply_text(f"Task #{task_num} is already {task['status']}.")
        return

    with db._conn() as con:
        con.execute("UPDATE tasks SET planned_start = ? WHERE id = ?", (planned_start, task_id))

    now_local = _local_now()
    h, m = planned_start.split(":")
    run_at_local = now_local.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
    if run_at_local <= now_local:
        await update.message.reply_text(f"#{task_num} {task['description']} — {planned_start} is in the past, no reminder set.")
        return

    run_at_utc = run_at_local.astimezone(timezone.utc)
    _schedule_start_ping(context.application, task_id, run_at_utc)
    db.log_event("rescheduled", task_id=task_id, payload=planned_start)
    await update.message.reply_text(f"#{task_num} *{task['description']}* → {planned_start}. Reminder set.", parse_mode="Markdown")


# ---------- /begin — start (or resume) any task that isn't running or done ----------

_BEGIN_STATUS_LABEL = {"stuck": "stuck", "paused": "paused", "missed": "missed", "skipped": "skipped"}


async def cmd_begin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    today = _today()
    tasks = [t for t in db.get_tasks_for_date(today) if t["status"] not in _RUNNING_OR_DONE]
    if not tasks:
        await update.message.reply_text("Nothing open to start. Use /todo to add one.")
        return
    buttons = []
    for t in tasks:
        label = t["description"]
        note = _BEGIN_STATUS_LABEL.get(t["status"])
        if note:
            label += f" ({note})"
        elif t["planned_start"]:
            label += f" @ {t['planned_start']}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"begin_task:{t['id']}")])
    await update.message.reply_text("Which task are you starting now?", reply_markup=InlineKeyboardMarkup(buttons))


# ---------- /snooze ----------

async def cmd_snooze(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    today = _today()
    tasks = db.get_tasks_for_date(today)
    db.log_event("command", payload="/snooze")

    # Find the next planned task with a start time that hasn't fired yet
    pending = [
        t for t in tasks
        if t["status"] == "planned" and t["planned_start"]
    ]
    if not pending:
        await update.message.reply_text("No upcoming start pings to snooze.")
        return

    task = pending[0]
    snooze_until = _utc_now() + timedelta(minutes=config.SNOOZE_MINUTES)
    _schedule_start_ping(context.application, task["id"], snooze_until)
    local_time = snooze_until.astimezone(config.TZ).strftime("%H:%M")
    db.log_event("snoozed", task_id=task["id"])
    await update.message.reply_text(f"Pushed {config.SNOOZE_MINUTES} min. Next nudge at {local_time}.")


# ---------- /silence_today ----------

async def cmd_silence_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    today = _today()
    db.set_day_flag(today, "silenced", 1)
    db.log_event("silence_today")

    # Cancel any pending jobs for today
    current_jobs = context.application.job_queue.jobs()
    for job in current_jobs:
        if job.name and (job.name.startswith("start_ping_") or job.name.startswith("endpoint_ping_")):
            job.schedule_removal()

    h, m = config.evening_time()
    await update.message.reply_text(
        f"Quiet for the rest of today. Back tomorrow at {h:02d}:{m:02d}."
    )


# ---------- morning plan (free text) ----------

def _lock_morning_plan_text(tasks: list) -> str:
    lines = ["Locked in:"]
    for i, t in enumerate(tasks, 1):
        time_label = t["planned_start"] if t["planned_start"] else "unscheduled"
        lines.append(f"{i}. {t['description']} — {time_label}")
    if any(t["planned_start"] for t in tasks):
        lines.append("I'll nudge you at each start time.")
    else:
        lines.append("No start times — you're on your own schedule today.")
    return "\n".join(lines)


async def _finalize_morning_plan(update_or_query, context: ContextTypes.DEFAULT_TYPE, capped: bool = False) -> None:
    """Show 'so far' or 'locked in' after adding tasks. Works from message or callback."""
    today = _today()
    all_tasks = db.get_tasks_for_date(today)
    total = len(all_tasks)
    extra = "\nCapped at 3 — the rest can wait. Finishing beats listing." if capped else ""

    send = (
        update_or_query.edit_message_text
        if hasattr(update_or_query, "edit_message_text")
        else update_or_query.message.reply_text
    )

    if total >= 3:
        db.set_day_flag(today, "morning_done", 1)
        await send(_lock_morning_plan_text(all_tasks) + extra)
    else:
        remaining = 3 - total
        task_word = "task" if remaining == 1 else "tasks"
        list_lines = ["So far:"]
        for i, t in enumerate(all_tasks, 1):
            time_label = t["planned_start"] if t["planned_start"] else "unscheduled"
            list_lines.append(f"{i}. {t['description']} — {time_label}")
        list_lines.append(f"\nAdd up to {remaining} more {task_word}, or /done to lock in.{extra}")
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("✓ Lock in", callback_data="lock_morning:0")]])
        await send("\n".join(list_lines), reply_markup=keyboard)


async def _prompt_confirm_unscheduled(update_or_query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show confirm prompt for the next queued unscheduled task."""
    queue = context.user_data.get("confirm_queue", [])
    if not queue:
        await _finalize_morning_plan(update_or_query, context)
        return
    desc = queue[0]
    send = (
        update_or_query.edit_message_text
        if hasattr(update_or_query, "edit_message_text")
        else update_or_query.message.reply_text
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("Add (no time)", callback_data="confirm_add:1"),
        InlineKeyboardButton("Skip", callback_data="confirm_add:0"),
    ]])
    await send(f"'{desc}' has no time set. Add it anyway?\nUse /at later to set a time, or /begin to start it.", reply_markup=keyboard)


async def handle_morning_plan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Parse the user's free-text morning plan into tasks."""
    today = _today()
    state = db.get_day_state(today)
    if state["morning_done"]:
        return

    existing = db.get_tasks_for_date(today)
    slots_left = 3 - len(existing)
    if slots_left <= 0:
        # Auto-lock — shouldn't normally reach here
        db.set_day_flag(today, "morning_done", 1)
        return

    raw_lines = [l.strip() for l in update.message.text.strip().splitlines() if l.strip()]
    if not raw_lines:
        return

    capped = len(raw_lines) > slots_left
    lines = raw_lines[:slots_left]

    now_local = _local_now()
    added = 0
    unscheduled_queue: list[str] = []

    for line in lines:
        desc, planned_start = _parse_task_line(line)
        if not desc:
            continue

        if planned_start:
            h, m = planned_start.split(":")
            start_local = now_local.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
            if start_local <= now_local:
                planned_start = None

        if not planned_start:
            unscheduled_queue.append(desc)
            continue

        task_id = db.add_task(today, desc, planned_start, config.DEFAULT_TIMER_MINUTES)
        db.log_event("task_added", task_id=task_id, payload=desc)
        added += 1

        h, m = planned_start.split(":")
        run_at_local = now_local.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
        run_at_utc = run_at_local.astimezone(timezone.utc)
        _schedule_start_ping(context.application, task_id, run_at_utc)

    if added == 0 and not unscheduled_queue:
        await update.message.reply_text(
            "Couldn't parse that — send one task per line, e.g. `Call dentist @ 14:00`.",
            parse_mode="Markdown",
        )
        return

    context.user_data["confirm_queue"] = (context.user_data.get("confirm_queue") or []) + unscheduled_queue
    context.user_data["confirm_capped"] = capped

    if unscheduled_queue:
        await _prompt_confirm_unscheduled(update, context)
    else:
        await _finalize_morning_plan(update, context, capped=capped)


# ---------- /todo (add task to today) ----------

async def cmd_todo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    text = " ".join(context.args).strip() if context.args else ""
    if not text:
        await update.message.reply_text(
            "Usage: `/todo Call dentist @ 14:00`\n"
            "The `@ HH:MM` time is optional.",
            parse_mode="Markdown",
        )
        return
    desc, planned_start = _parse_task_line(text)

    # Drop times already in the past
    now_local = _local_now()
    if planned_start:
        h, ms = planned_start.split(":")
        start_local = now_local.replace(hour=int(h), minute=int(ms), second=0, microsecond=0)
        if start_local <= now_local:
            planned_start = None

    task_id = db.add_task(_today(), desc, planned_start, config.DEFAULT_TIMER_MINUTES)
    db.log_event("command", task_id=task_id, payload="/todo")

    # Schedule start ping if a future time was given
    if planned_start:
        h, ms = planned_start.split(":")
        run_at_local = now_local.replace(hour=int(h), minute=int(ms), second=0, microsecond=0)
        _schedule_start_ping(context.application, task_id, run_at_local.astimezone(timezone.utc))

    today = _today()
    state = db.get_day_state(today)

    # During morning planning phase: integrate into the plan instead of asking for estimate
    if not state["morning_done"]:
        all_tasks = db.get_tasks_for_date(today)
        if len(all_tasks) >= 3:
            db.set_day_flag(today, "morning_done", 1)
            await update.message.reply_text(_lock_morning_plan_text(all_tasks))
        else:
            remaining = 3 - len(all_tasks)
            task_word = "task" if remaining == 1 else "tasks"
            lines = ["So far:"]
            for i, t in enumerate(all_tasks, 1):
                time_label = t["planned_start"] if t["planned_start"] else "unscheduled"
                lines.append(f"{i}. {t['description']} — {time_label}")
            lines.append(f"\nAdd up to {remaining} more {task_word}, or /done to lock in.")
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("✓ Lock in", callback_data="lock_morning:0")]])
            await update.message.reply_text("\n".join(lines), reply_markup=keyboard)
        return

    # Mid-day: ask for time estimate so we can set a useful timer
    ai_estimate = await asyncio.to_thread(ai_mod.estimate_task_minutes, desc)
    time_str = f" @ {planned_start}" if planned_start else ""

    if ai_estimate is not None:
        db.update_task_time_estimates(task_id, ai_estimate=ai_estimate)
        prompt = (
            f"✅ Added: *{desc}*{time_str}\n\n"
            f"\U0001f916 AI estimate: ~{ai_estimate} min. How long do you think it'll take? "
            f"(reply with minutes, or /skip)"
        )
    else:
        prompt = (
            f"✅ Added: *{desc}*{time_str}\n\n"
            f"How long do you think this will take? (reply with minutes, or /skip)"
        )

    msg = await update.message.reply_text(prompt, parse_mode="Markdown")
    _add_pending(context, "pending_estimates", msg.message_id, task_id=task_id)


def _cancel_job(context: ContextTypes.DEFAULT_TYPE, name: str) -> None:
    for job in context.application.job_queue.jobs():
        if job.name == name:
            job.schedule_removal()


def _mark_task_done(context: ContextTypes.DEFAULT_TYPE, task_id: int) -> None:
    """Core DB + job-cancellation side effects of finishing a task, whether
    it was running (started) or never started at all. No messaging —
    callers own the reply since they differ (plain message vs. inline
    keyboard callback). Cancelling a job that was never scheduled is a
    harmless no-op, so both job names are always cancelled."""
    now_utc = _utc_now()
    db.update_task_status(task_id, "done", completed_at=now_utc.isoformat())
    db.log_event("task_done", task_id=task_id)
    _cancel_job(context, f"endpoint_ping_{task_id}")
    _cancel_job(context, f"start_ping_{task_id}")


async def _ask_actual_time(context: ContextTypes.DEFAULT_TYPE, chat_id: int, task_id: int) -> None:
    # chat_id kept for signature compat; sends go to the ADHD Tasks topic
    msg = await context.bot.send_message(text="How long did that actually take? (minutes, or /skip)", **config.send_kwargs())
    _add_pending(context, "pending_actuals", msg.message_id, task_id=task_id)


def _pause_task_core(context: ContextTypes.DEFAULT_TYPE, task_id: int) -> None:
    db.update_task_status(task_id, "paused")
    db.log_event("task_paused", task_id=task_id)
    _cancel_job(context, f"endpoint_ping_{task_id}")


def _running_task_keyboard_for(tasks: list, callback_prefix: str) -> InlineKeyboardMarkup:
    buttons = []
    for t in tasks:
        label = t["description"]
        if t["started_at"]:
            started_local = datetime.fromisoformat(t["started_at"]).astimezone(config.TZ).strftime("%H:%M")
            label += f" (started {started_local})"
        buttons.append([InlineKeyboardButton(label, callback_data=f"{callback_prefix}:{t['id']}")])
    return InlineKeyboardMarkup(buttons)


# ---------- /pause_task — pause a running timer without finishing it ----------

async def cmd_pause_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    today = _today()
    running = [t for t in db.get_tasks_for_date(today) if t["status"] == "started"]
    if not running:
        await update.message.reply_text("No running task to pause.")
        return
    if len(running) == 1:
        task = running[0]
        _pause_task_core(context, task["id"])
        await update.message.reply_text(f"Paused: {task['description']}. Use /begin to resume it.")
        return
    await update.message.reply_text(
        "Multiple timers running — which one are you pausing?",
        reply_markup=_running_task_keyboard_for(running, "pause_task"),
    )


async def _complete_started_task(update: Update, context: ContextTypes.DEFAULT_TYPE, task: dict) -> None:
    """Mark a 'started' task done immediately (early finish), same as the
    endpoint-ping 'Done' button, then ask for actual time spent."""
    task_id = task["id"]
    _mark_task_done(context, task_id)
    await update.message.reply_text(f"One down: {task['description']}.")
    await _ask_actual_time(context, update.effective_chat.id, task_id)


# ---------- /done (finish a running task, or lock morning plan) ----------

async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    today = _today()

    started = [t for t in db.get_tasks_for_date(today) if t["status"] == "started"]
    if len(started) == 1:
        await _complete_started_task(update, context, started[0])
        return
    if len(started) > 1:
        await update.message.reply_text(
            "Multiple timers running — which one did you finish?",
            reply_markup=_running_task_keyboard_for(started, "finish_task"),
        )
        return

    state = db.get_day_state(today)
    if state["morning_done"]:
        await update.message.reply_text("Morning plan already locked in. No task currently running.")
        return
    tasks = db.get_tasks_for_date(today)
    if not tasks:
        await update.message.reply_text("No tasks yet — send your list first.")
        return
    db.set_day_flag(today, "morning_done", 1)
    await update.message.reply_text(_lock_morning_plan_text(tasks))


# ---------- /complete_task (finish any open task, not just a running one) ----------

_OPEN_STATUSES = ("planned", "started", "stuck", "paused")
_OPEN_STATUS_LABEL = {"started": "running", "stuck": "stuck", "paused": "paused"}


def _open_task_keyboard(tasks: list) -> InlineKeyboardMarkup:
    buttons = []
    for t in tasks:
        label = t["description"]
        note = _OPEN_STATUS_LABEL.get(t["status"])
        if note:
            label += f" ({note})"
        elif t["planned_start"]:
            label += f" @ {t['planned_start']}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"complete_task:{t['id']}")])
    return InlineKeyboardMarkup(buttons)


async def cmd_complete_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    today = _today()
    tasks = [t for t in db.get_tasks_for_date(today) if t["status"] in _OPEN_STATUSES]
    if not tasks:
        await update.message.reply_text("No open tasks — everything today is already done, skipped, or missed.")
        return
    await update.message.reply_text("Which task did you finish?", reply_markup=_open_task_keyboard(tasks))


# ---------- evening reply (free text) ----------

async def handle_evening_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Capture the one-line evening reflection."""
    today = _today()
    state = db.get_day_state(today)
    if not state["evening_prompted"] or state["evening_done"]:
        return  # not in evening state

    # Only accept after 18:00 local to avoid swallowing daytime messages
    if _local_now().hour < 18:
        return

    text = update.message.text.strip()
    db.log_event("evening_response", payload=text)
    db.set_day_flag(today, "evening_done", 1)
    await update.message.reply_text("Logged. See you tomorrow.")


# ---------- /lesson + /lessons ----------

async def cmd_lesson(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    msg = await update.message.reply_text("What went well today?")
    context.user_data["lesson_pending"] = {
        "stage": "went_well", "msg_id": msg.message_id, "ts": _utc_now().isoformat(),
    }


async def cmd_lessons(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    lessons = db.get_lessons(limit=7)
    if not lessons:
        await update.message.reply_text("No lessons logged yet. Use /lesson to add one.")
        return
    parts = []
    for lesson in lessons:
        parts.append(f"*{lesson['date']}*")
        parts.append(f"✓ {lesson['went_well']}")
        parts.append(f"△ {lesson['to_improve']}")
        if lesson["learning"]:
            parts.append(f"💡 {lesson['learning']}")
        parts.append("")
    await update.message.reply_text("\n".join(parts).strip(), parse_mode="Markdown")


# ---------- /weekly ----------

_STATUS_ICON = {
    "done": "✓", "missed": "✗", "skipped": "–", "stuck": "?",
    "started": "▶", "planned": "○", "paused": "⏸",
}


async def cmd_weekly(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    end = _local_now().date()
    start = end - timedelta(days=6)
    tasks = db.get_tasks_for_range(start.isoformat(), end.isoformat())
    lessons = db.get_lessons_for_range(start.isoformat(), end.isoformat())
    db.log_event("command", payload="/weekly")

    if not tasks:
        await update.message.reply_text(f"No tasks logged {start.isoformat()}–{end.isoformat()}.")
        return

    counts = Counter(t["status"] for t in tasks)
    total = len(tasks)
    lines = [f"*Week of {start.isoformat()} – {end.isoformat()}*", ""]
    lines.append(f"{total} task(s): " + ", ".join(
        f"{_STATUS_ICON.get(s, '○')} {counts[s]} {s}" for s in
        ("done", "missed", "skipped", "stuck") if counts.get(s)
    ))

    stuck = [t for t in tasks if t["status"] == "stuck"]
    if stuck:
        lines.append("\n*Stuck on:*")
        for t in stuck:
            lines.append(f"– {t['description']} ({t['date']})")
        lines.append("What made these different from the ones that got done?")

    if lessons:
        lines.append("\n*Lessons this week:*")
        for lesson in lessons:
            lines.append(f"*{lesson['date']}* — {lesson['went_well']}")
            if lesson["learning"]:
                lines.append(f"  💡 {lesson['learning']}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ---------- /trends ----------

async def cmd_trends(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    tasks = db.get_tasks_with_actuals(limit=200)
    db.log_event("command", payload="/trends")

    if not tasks:
        await update.message.reply_text(
            "No completed tasks with a logged actual time yet — that gets recorded "
            "when you answer 'How long did that actually take?' after finishing a task."
        )
        return

    user_diffs = [(t["actual_minutes"] - t["user_estimate_minutes"], t) for t in tasks if t["user_estimate_minutes"]]
    ai_diffs = [(t["actual_minutes"] - t["ai_estimate_minutes"], t) for t in tasks if t["ai_estimate_minutes"]]

    lines = [f"*Estimate accuracy* (last {len(tasks)} completed task(s) with logged time)"]

    if user_diffs:
        avg = sum(d for d, _ in user_diffs) / len(user_diffs)
        direction = "underestimate" if avg > 0 else "overestimate"
        lines.append(f"\nYour estimates: you {direction} by {abs(avg):.0f} min on average ({len(user_diffs)} task(s)).")
        worst = sorted(user_diffs, key=lambda x: -abs(x[0]))[:3]
        for d, t in worst:
            sign = "+" if d >= 0 else ""
            lines.append(f"  {t['description']} — est {t['user_estimate_minutes']}, actual {t['actual_minutes']} ({sign}{d})")

    if ai_diffs:
        avg = sum(d for d, _ in ai_diffs) / len(ai_diffs)
        direction = "underestimates" if avg > 0 else "overestimates"
        lines.append(f"\nAI {direction} by {abs(avg):.0f} min on average ({len(ai_diffs)} task(s)).")

    if not user_diffs and not ai_diffs:
        lines.append("\nNo estimates logged alongside actual times yet.")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def handle_lesson_response(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    pending = context.user_data.get("lesson_pending")
    if not pending:
        return
    stage = pending["stage"]
    text = update.message.text.strip()

    if stage == "went_well":
        pending["went_well"] = text
        pending["stage"] = "to_improve"
        msg = await update.message.reply_text("What could go better?")
        pending["msg_id"] = msg.message_id
        pending["ts"] = _utc_now().isoformat()

    elif stage == "to_improve":
        pending["to_improve"] = text
        pending["stage"] = "learning"
        msg = await update.message.reply_text("What did you learn, if anything? (/skip to leave blank)")
        pending["msg_id"] = msg.message_id
        pending["ts"] = _utc_now().isoformat()

    elif stage == "learning":
        today = _today()
        context.user_data.pop("lesson_pending", None)
        learning = None if text.lower() in ("skip", "/skip", "") else text
        db.add_lesson(today, pending.get("went_well", ""), pending.get("to_improve", ""), learning)
        db.log_event("lesson_logged", payload=today)
        await update.message.reply_text("Lesson logged.")


# ---------- time estimate collection ----------

def _padded_timer(user_estimate: int) -> int:
    """Add ~20% padding to user estimate, rounded to nearest 5 min, minimum +5."""
    import math
    padding = max(5, math.ceil(user_estimate * 0.20 / 5) * 5)
    return user_estimate + padding


def _parse_estimate_minutes(text: str) -> int | None:
    """Parse user's time estimate. Handles '30', '1:30' (1h30m), '1h', '1.5h'."""
    text = text.strip().lower()
    # H:MM format → hours * 60 + minutes
    m = re.fullmatch(r"(\d+):(\d{2})", text)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    # Nh or NhMm
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*h(?:r|ours?)?(?:\s*(\d+)\s*m?)?", text)
    if m:
        hours = float(m.group(1))
        mins = int(m.group(2)) if m.group(2) else 0
        return round(hours * 60) + mins
    # Plain number (minutes)
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None


async def _handle_user_estimate(update: Update, context: ContextTypes.DEFAULT_TYPE, task_id: int) -> None:
    text = update.message.text.strip()
    if text.lower() in ("skip", "/skip", ""):
        await update.message.reply_text("No estimate saved — timer stays at default.")
        return
    minutes = _parse_estimate_minutes(text)
    if minutes is None:
        msg = await update.message.reply_text("Enter a number of minutes (e.g. `30`, `1:30`, `1h`), or /skip.", parse_mode="Markdown")
        _add_pending(context, "pending_estimates", msg.message_id, task_id=task_id)
        return
    timer = _padded_timer(minutes)
    db.update_task_time_estimates(task_id, user_estimate=minutes)
    db.update_timer_minutes(task_id, timer)
    task = db.get_task(task_id)
    ai_est = task["ai_estimate_minutes"] if task else None
    if ai_est:
        diff = minutes - ai_est
        sign = "+" if diff >= 0 else ""
        await update.message.reply_text(
            f"Got it — your estimate: {minutes} min (AI: {ai_est} min, diff {sign}{diff}). Timer set to {timer} min."
        )
    else:
        await update.message.reply_text(f"Got it — your estimate: {minutes} min. Timer set to {timer} min.")


async def _handle_actual_time(update: Update, context: ContextTypes.DEFAULT_TYPE, task_id: int) -> None:
    text = update.message.text.strip()
    if text.lower() not in ("skip", "/skip", ""):
        digits = re.sub(r"[^\d]", "", text)
        if not digits:
            msg = await update.message.reply_text("Enter minutes (e.g. `45`), or /skip.", parse_mode="Markdown")
            _add_pending(context, "pending_actuals", msg.message_id, task_id=task_id)
            return
        actual = int(digits)
        db.update_task_time_estimates(task_id, actual=actual)
        task = db.get_task(task_id)
        parts = [f"Actual: {actual} min."]
        if task:
            if task["user_estimate_minutes"]:
                d = actual - task["user_estimate_minutes"]
                parts.append(f"You estimated {task['user_estimate_minutes']} min ({'+'if d>=0 else ''}{d}).")
            if task["ai_estimate_minutes"]:
                d = actual - task["ai_estimate_minutes"]
                parts.append(f"AI estimated {task['ai_estimate_minutes']} min ({'+'if d>=0 else ''}{d}).")
        await update.message.reply_text(" ".join(parts))

    msg = await update.message.reply_text("Note for next time? (or /skip)")
    _add_pending(context, "pending_outcomes", msg.message_id, task_id=task_id)


# ---------- outcome note after Done ----------

async def handle_outcome_note(update: Update, context: ContextTypes.DEFAULT_TYPE, task_id: int) -> None:
    """Store the optional outcome note after marking a task done."""
    note = update.message.text.strip()
    if note.lower() not in ("skip", "/skip", ""):
        db.update_task_status(task_id, "done", outcome_note=note)
    await update.message.reply_text("Noted.")


# ---------- free-text router ----------

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return

    # A prompt only gets fulfilled by an explicit reply to that exact
    # message — plain text never auto-targets a dangling question, and stale
    # prompts (>3h old) are pruned rather than swallowing unrelated text.
    reply_to_id = (update.message.reply_to_message.message_id
                   if update.message.reply_to_message else None)

    lesson_pending = context.user_data.get("lesson_pending")
    if lesson_pending and _is_fresh(lesson_pending) and reply_to_id == lesson_pending["msg_id"]:
        await handle_lesson_response(update, context)
        return

    entry = _pop_pending_by_reply(context, "pending_estimates", reply_to_id)
    if entry:
        await _handle_user_estimate(update, context, entry["task_id"])
        return

    entry = _pop_pending_by_reply(context, "pending_actuals", reply_to_id)
    if entry:
        await _handle_actual_time(update, context, entry["task_id"])
        return

    entry = _pop_pending_by_reply(context, "pending_outcomes", reply_to_id)
    if entry:
        await handle_outcome_note(update, context, entry["task_id"])
        return

    today = _today()
    state = db.get_day_state(today)

    # Evening reply takes priority once the evening prompt has actually gone
    # out — otherwise, on a day where morning_done never got set (missed
    # prompt, /skip never sent, etc.), the reflection reply gets misread as
    # a new morning task line.
    if state["evening_prompted"] and not state["evening_done"] and _local_now().hour >= 18:
        await handle_evening_reply(update, context)
        return

    if not state["morning_done"]:
        await handle_morning_plan(update, context)
        return

    # Plan is locked, not yet evening — give feedback instead of silently ignoring
    await update.message.reply_text(
        "Plan is locked in. Use /today to see your tasks, or /todo to add one."
    )


# ---------- inline callback queries ----------

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return

    query = update.callback_query
    await query.answer()

    action, task_id_str = query.data.split(":", 1)

    # Actions that don't use a DB task_id
    if action == "lock_morning":
        today = _today()
        state = db.get_day_state(today)
        if state["morning_done"]:
            await query.edit_message_text("Already locked in.")
            return
        tasks = db.get_tasks_for_date(today)
        if not tasks:
            await query.edit_message_text("No tasks to lock in yet.")
            return
        db.set_day_flag(today, "morning_done", 1)
        await query.edit_message_text(_lock_morning_plan_text(tasks))
        return

    if action == "confirm_add":
        choice = int(task_id_str)
        queue: list[str] = context.user_data.get("confirm_queue", [])
        if not queue:
            await query.answer("Nothing to confirm.")
            return
        desc = queue.pop(0)
        context.user_data["confirm_queue"] = queue
        capped = context.user_data.get("confirm_capped", False)
        if choice == 1:
            today = _today()
            new_id = db.add_task(today, desc, None, config.DEFAULT_TIMER_MINUTES)
            db.log_event("task_added", task_id=new_id, payload=desc)
        if queue:
            await _prompt_confirm_unscheduled(query, context)
        else:
            await _finalize_morning_plan(query, context, capped=capped)
        return

    task_id = int(task_id_str)
    task = db.get_task(task_id)
    if task is None:
        await query.edit_message_text("Task not found.")
        return

    now_utc = _utc_now()

    if action == "finish_task":
        if task["status"] != "started":
            await query.edit_message_text(f"Already {task['status']}.")
            return
        _mark_task_done(context, task_id)
        await query.edit_message_text(f"One down: {task['description']}.")
        await _ask_actual_time(context, query.message.chat_id, task_id)
        return

    if action == "complete_task":
        if task["status"] not in _OPEN_STATUSES:
            await query.edit_message_text(f"Already {task['status']}.")
            return
        _mark_task_done(context, task_id)
        await query.edit_message_text(f"One down: {task['description']}.")
        await _ask_actual_time(context, query.message.chat_id, task_id)
        return

    if action == "pause_task":
        if task["status"] != "started":
            await query.edit_message_text(f"Already {task['status']}.")
            return
        _pause_task_core(context, task_id)
        await query.edit_message_text(f"Paused: {task['description']}. Use /begin to resume it.")
        return

    # --- start ping responses ---
    if action == "start_yes":
        if task["status"] in _RUNNING_OR_DONE:
            await query.edit_message_text(f"Already {task['status']}.")
            return
        db.update_task_status(task_id, "started", started_at=now_utc.isoformat())
        db.log_event("timer_started", task_id=task_id)
        endpoint_at = now_utc + timedelta(minutes=task["timer_minutes"])
        _schedule_endpoint_ping(context.application, task_id, endpoint_at)
        await query.edit_message_text(f"Timer running — {task['timer_minutes']} min. Go.")

    elif action == "start_snooze":
        if task["status"] in _RUNNING_OR_DONE:
            await query.edit_message_text(f"Already {task['status']}.")
            return
        snooze_at = now_utc + timedelta(minutes=config.SNOOZE_MINUTES)
        _schedule_start_ping(context.application, task_id, snooze_at)
        local_time = snooze_at.astimezone(config.TZ).strftime("%H:%M")
        db.log_event("snoozed", task_id=task_id)
        await query.edit_message_text(f"Pushed {config.SNOOZE_MINUTES}. Next nudge at {local_time}.")

    elif action == "start_skip":
        if task["status"] in _RUNNING_OR_DONE:
            await query.edit_message_text(f"Already {task['status']}.")
            return
        db.update_task_status(task_id, "skipped")
        db.log_event("task_skipped", task_id=task_id)
        await query.edit_message_text("Skipped — logged, not judged.")

    # --- endpoint ping responses ---
    elif action == "end_done":
        if task["status"] != "started":
            await query.edit_message_text(f"Already {task['status']}.")
            return
        _mark_task_done(context, task_id)
        await query.edit_message_text("One down.")
        await _ask_actual_time(context, query.message.chat_id, task_id)

    elif action == "end_more":
        if task["status"] != "started":
            await query.edit_message_text(f"Already {task['status']}.")
            return
        new_endpoint = now_utc + timedelta(minutes=task["timer_minutes"])
        _schedule_endpoint_ping(context.application, task_id, new_endpoint)
        db.log_event("more_time", task_id=task_id)
        await query.edit_message_text(f"Another {task['timer_minutes']}. Keep going.")

    elif action == "end_stuck":
        if task["status"] != "started":
            await query.edit_message_text(f"Already {task['status']}.")
            return
        db.update_task_status(task_id, "stuck")
        db.log_event("task_stuck", task_id=task_id)
        await query.edit_message_text(
            "Parked. We'll dig into what 'stuck' means in the weekly review. Move on?"
        )

    elif action == "begin_task":
        if task["status"] in _RUNNING_OR_DONE:
            await query.edit_message_text(f"Task is already {task['status']}.")
            return
        minutes = task["timer_minutes"]
        await query.edit_message_text(
            f"Start: {task['description']}. Begin a {minutes}-min timer?",
            reply_markup=_start_ping_keyboard(task_id),
        )
