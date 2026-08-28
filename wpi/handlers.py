"""
All Telegram command and callback handlers.

Conversation states
-------------------
/start onboarding:  CHOOSE_TYPE → ENTER_CHALLENGE → ENTER_WANT → CONFIRM
/log urge:          LOG_WEEK → LOG_PICK → LOG_OUTCOME
                    → [LOG_WEEK1|LOG_WEEK2|LOG_WEEK3] (week-skill question, optional)
                    → LOG_TRIGGER → LOG_INTENSITY → LOG_NOTES
/reset:             RESET_CONFIRM

Evening check-in (scheduler-driven):
  Uses user_data["checkin"] dict to pass state between inline-keyboard steps.
  Free-text collection uses user_data["awaiting"] = "obs" | "reflect" | "sleep".
"""

import logging
from datetime import date, datetime
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from . import db
from .config import OWNER_CHAT_ID, load_program, max_week as config_max_week

logger = logging.getLogger(__name__)

# ── conversation state constants ──────────────────────────────────────────────
CHOOSE_TYPE, ENTER_CHALLENGE, ENTER_WANT, CONFIRM = range(4)
LOG_WEEK, LOG_PICK, LOG_OUTCOME, LOG_WEEK1, LOG_WEEK2, LOG_WEEK3, \
    LOG_TRIGGER, LOG_INTENSITY, LOG_NOTES = range(4, 13)
RESET_CONFIRM = 13


# ── helpers ───────────────────────────────────────────────────────────────────

def _kb(buttons: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, callback_data=data) for label, data in row]
         for row in buttons]
    )


def _type_label(t: str) -> str:
    return {"i_will": "I Will", "i_wont": "I Won't", "i_want": "I Want"}.get(t, t)


def _adherence_emoji(a: Optional[str]) -> str:
    return {"yes": "✅", "no": "❌", "partial": "〰️"}.get(a or "", "—")


async def _no_cycle(update: Update) -> None:
    await update.effective_message.reply_text(
        "No active cycle. Use /start to begin a new cycle."
    )


async def _get_week_data(cycle: dict) -> dict:
    program = load_program()
    return program.get(cycle["current_week"], {})


# ── /start onboarding ─────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    cycle = await db.get_active_cycle(user_id)
    if cycle:
        await update.message.reply_text(
            f"You already have an active cycle (Week {cycle['current_week']}).\n"
            "Use /status to see your progress or /reset to start fresh."
        )
        return ConversationHandler.END

    kb = _kb([
        [("I Will (add a habit)", "start:type:i_will")],
        [("I Won't (break a habit)", "start:type:i_wont")],
        [("I Want (pursue a goal)", "start:type:i_want")],
    ])
    await update.message.reply_text(
        "Welcome to the Willpower Instinct tracker.\n\n"
        "Which power is your focus this cycle?",
        reply_markup=kb,
    )
    return CHOOSE_TYPE


async def start_choose_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    choice = query.data.split(":")[-1]
    context.user_data["challenge_type"] = choice
    label = _type_label(choice)
    await query.edit_message_text(
        f"Good — your challenge power is *{label}*.\n\n"
        f"Describe your specific challenge in one sentence.\n"
        f"Example: \"I will meditate for 10 minutes every morning.\"",
        parse_mode=ParseMode.MARKDOWN,
    )
    return ENTER_CHALLENGE


async def start_enter_challenge(update: Update,
                                context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["challenge_text"] = update.message.text.strip()
    await update.message.reply_text(
        "Now your *I Want* anchor — the deeper reason behind this challenge.\n"
        "What long-term outcome are you really after?\n"
        "Example: \"I want to feel calmer and more present.\"",
        parse_mode=ParseMode.MARKDOWN,
    )
    return ENTER_WANT


async def start_enter_want(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["i_want_anchor"] = update.message.text.strip()
    ct = context.user_data["challenge_type"]
    ch = context.user_data["challenge_text"]
    iw = context.user_data["i_want_anchor"]
    kb = _kb([
        [("Yes, start the cycle", "start:confirm:yes"),
         ("Cancel", "start:confirm:no")],
    ])
    await update.message.reply_text(
        f"Here's your cycle:\n\n"
        f"*Challenge ({_type_label(ct)}):* {ch}\n"
        f"*I Want anchor:* {iw}\n\n"
        "Start Week 1?",
        reply_markup=kb,
        parse_mode=ParseMode.MARKDOWN,
    )
    return CONFIRM


async def start_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if "no" in query.data:
        await query.edit_message_text("Cancelled. Use /start when you're ready.")
        context.user_data.clear()
        return ConversationHandler.END

    user_id = update.effective_user.id
    cycle_id = await db.create_cycle(
        user_id,
        context.user_data["challenge_type"],
        context.user_data["challenge_text"],
        context.user_data["i_want_anchor"],
    )
    context.user_data.clear()
    program = load_program()
    w1 = program.get(1, {})
    await query.edit_message_text(
        f"Cycle started! You're on *Week 1: {w1.get('title', '')}*.\n\n"
        f"_{w1.get('theme', '')}_\n\n"
        "Use /week to see this week's exercises, /log to capture an urge, "
        "and /today to see your daily status.",
        parse_mode=ParseMode.MARKDOWN,
    )
    return ConversationHandler.END


async def start_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("Setup cancelled.")
    return ConversationHandler.END


# ── /log — in-the-moment urge capture ────────────────────────────────────────

_LOG_KEYS = (
    "log_cycle_id", "log_challenge_id", "log_challenge_type",
    "log_skill_week", "log_outcome", "log_gave_in",
    "log_i_want_recalled", "log_breathing", "log_recovery",
    "log_trigger", "log_intensity",
)


def _challenge_buttons(challenges: list) -> list:
    return [
        [(f"{_type_label(c['challenge_type'])}: {c['challenge_text'][:50]}",
          f"log:pick:{c['id']}:{c['challenge_type']}")]
        for c in challenges
    ]


async def cmd_log(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    cycle = await db.get_active_cycle(user_id)
    if not cycle:
        await _no_cycle(update)
        return ConversationHandler.END
    context.user_data["log_cycle_id"] = cycle["id"]

    program = load_program()
    buttons = [[("Just an urge / temptation", "log:week:0")]]
    for n in range(1, cycle["current_week"] + 1):
        title = program.get(n, {}).get("title", f"Week {n}")
        buttons.append([(f"Week {n}: {title}", f"log:week:{n}")])

    await update.message.reply_text(
        "Which week's skill are you logging with?",
        reply_markup=_kb(buttons),
    )
    return LOG_WEEK


async def log_pick_week(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    n = int(query.data.split(":")[-1])
    context.user_data["log_skill_week"] = n

    cycle_id = context.user_data["log_cycle_id"]
    challenges = await db.get_challenges(cycle_id)

    if n == 0:
        header = "Which challenge are you logging for?"
    else:
        program = load_program()
        week = program.get(n, {})
        microscope = week.get("microscope", {}).get("text", "").strip()
        experiment = week.get("experiment", {}).get("text", "").strip()
        header = (
            f"*Week {n}: {week.get('title', '')}*\n\n"
            f"🔬 *Under the Microscope:*\n{microscope}\n\n"
            f"💡 *This Week's Experiment:*\n{experiment}\n\n"
            "Which challenge are you logging for?"
        )

    if not challenges:
        # No named challenges — go straight to outcome with i_wont default
        context.user_data["log_challenge_type"] = "i_wont"
        kb = _kb([[("✓ Resisted", "log:outcome:resisted"), ("✗ Gave in", "log:outcome:gave_in")]])
        await query.edit_message_text(
            header + "\n\nWhat was the result?",
            reply_markup=kb,
            parse_mode=ParseMode.MARKDOWN,
        )
        return LOG_OUTCOME

    await query.edit_message_text(
        header,
        reply_markup=_kb(_challenge_buttons(challenges)),
        parse_mode=ParseMode.MARKDOWN,
    )
    return LOG_PICK


async def log_pick_challenge(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")  # log:pick:<id>:<type>
    challenge_id = int(parts[2])
    challenge_type = parts[3]
    context.user_data["log_challenge_id"] = challenge_id
    context.user_data["log_challenge_type"] = challenge_type

    if challenge_type == "i_will":
        kb = _kb([[
            ("▶ Started", "log:outcome:started"),
            ("✅ Completed", "log:outcome:completed"),
            ("✗ Gave in", "log:outcome:gave_in"),
        ]])
    else:
        kb = _kb([[("✓ Resisted", "log:outcome:resisted"), ("✗ Gave in", "log:outcome:gave_in")]])

    await query.edit_message_text("What was the result?", reply_markup=kb)
    return LOG_OUTCOME


async def log_outcome(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    outcome = query.data.split(":")[-1]
    context.user_data["log_outcome"] = outcome
    context.user_data["log_gave_in"] = (outcome == "gave_in")

    skill_week = context.user_data.get("log_skill_week", 0)

    if skill_week == 1:
        kb = _kb([[("✅ Yes", "log:w1want:1"), ("❌ No", "log:w1want:0")]])
        await query.edit_message_text(
            "Did you pause to remember your *I Want* goal?",
            reply_markup=kb,
            parse_mode=ParseMode.MARKDOWN,
        )
        return LOG_WEEK1

    if skill_week == 2:
        kb = _kb([[("✅ Yes", "log:w2breath:1"), ("❌ No", "log:w2breath:0")]])
        await query.edit_message_text(
            "Did you pause and breathe (slow breathing)?",
            reply_markup=kb,
        )
        return LOG_WEEK2

    if skill_week == 3:
        kb = _kb([[
            ("😴 Sleep", "log:w3recovery:sleep"),
            ("🚶 Exercise", "log:w3recovery:exercise"),
            ("🧘 Strategic rest", "log:w3recovery:rest"),
            ("— None", "log:w3recovery:none"),
        ]])
        await query.edit_message_text(
            "Which recovery action did you take?",
            reply_markup=kb,
        )
        return LOG_WEEK3

    # Week 0 or 4-10 (STUB) — skip straight to trigger
    await query.edit_message_text(
        "What triggered this urge? Describe it briefly (or type *skip*).",
        parse_mode=ParseMode.MARKDOWN,
    )
    return LOG_TRIGGER


async def log_week1(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["log_i_want_recalled"] = int(query.data.split(":")[-1])
    await query.edit_message_text(
        "What triggered this urge? Describe it briefly (or type *skip*).",
        parse_mode=ParseMode.MARKDOWN,
    )
    return LOG_TRIGGER


async def log_week2(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["log_breathing"] = int(query.data.split(":")[-1])
    await query.edit_message_text(
        "What triggered this urge? Describe it briefly (or type *skip*).",
        parse_mode=ParseMode.MARKDOWN,
    )
    return LOG_TRIGGER


async def log_week3(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    val = query.data.split(":")[-1]
    context.user_data["log_recovery"] = None if val == "none" else val
    await query.edit_message_text(
        "What triggered this urge? Describe it briefly (or type *skip*).",
        parse_mode=ParseMode.MARKDOWN,
    )
    return LOG_TRIGGER


async def log_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    context.user_data["log_trigger"] = None if text.lower() == "skip" else text
    kb = _kb([
        [("1", "log:intensity:1"), ("2", "log:intensity:2"), ("3", "log:intensity:3"),
         ("4", "log:intensity:4"), ("5", "log:intensity:5")],
    ])
    await update.message.reply_text(
        "Intensity of the urge (1 = mild, 5 = overwhelming)?",
        reply_markup=kb,
    )
    return LOG_INTENSITY


async def log_intensity(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["log_intensity"] = int(query.data.split(":")[-1])
    await query.edit_message_text(
        "Any notes? (what helped / what didn't / what you noticed) — or type *skip*.",
        parse_mode=ParseMode.MARKDOWN,
    )
    return LOG_NOTES


async def log_notes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    notes = None if text.lower() == "skip" else text
    user_id = update.effective_user.id
    outcome = context.user_data.get("log_outcome")
    gave_in = context.user_data.get("log_gave_in")

    await db.log_urge(
        user_id=user_id,
        cycle_id=context.user_data["log_cycle_id"],
        challenge_id=context.user_data.get("log_challenge_id"),
        trigger_text=context.user_data.get("log_trigger"),
        gave_in=gave_in,
        intensity=context.user_data.get("log_intensity"),
        notes=notes,
        outcome=outcome,
        skill_week=context.user_data.get("log_skill_week"),
        i_want_recalled=context.user_data.get("log_i_want_recalled"),
        breathing_done=context.user_data.get("log_breathing"),
        recovery_action=context.user_data.get("log_recovery"),
    )

    if outcome == "gave_in":
        result = "Logged. You gave in — that's data, not failure."
    elif outcome == "completed":
        result = "Logged. Completed. That's real progress."
    elif outcome == "started":
        result = "Logged. You started — that's the hardest part."
    else:
        result = "Logged. You resisted. That's real."

    for k in _LOG_KEYS:
        context.user_data.pop(k, None)
    await update.message.reply_text(result)
    return ConversationHandler.END


async def log_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    for k in _LOG_KEYS:
        context.user_data.pop(k, None)
    await update.message.reply_text("Urge log cancelled.")
    return ConversationHandler.END


# ── /today ────────────────────────────────────────────────────────────────────

async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    cycle = await db.get_active_cycle(user_id)
    if not cycle:
        await _no_cycle(update)
        return

    entry = await db.get_daily_entry(cycle["id"])
    urges = await db.get_today_urges(cycle["id"])
    boosters = await db.get_today_boosters(cycle["id"])
    challenges = await db.get_challenges(cycle["id"])
    adherences = await db.get_challenge_adherences(cycle["id"])
    adherence_map = {a["challenge_id"]: a["adherence"] for a in adherences}
    program = load_program()
    week = program.get(cycle["current_week"], {})

    today_str = date.today().strftime("%A, %B %d")
    lines = [f"*{today_str} — Week {cycle['current_week']}: {week.get('title', '')}*\n"]

    if challenges:
        lines.append("*Challenges:*")
        for c in challenges:
            adh = adherence_map.get(c["id"])
            emoji = f" {_adherence_emoji(adh)}" if adh else ""
            lines.append(f"  {_type_label(c['challenge_type'])}: _{c['challenge_text'][:60]}_{emoji}")
    lines.append(f"I Want: _{cycle['i_want_anchor']}_\n")

    if entry:
        lines.append("*Today's check-in:*")
        if entry.get("morning_energy"):
            lines.append(f"  Morning energy: {'⚡' * entry['morning_energy']}")
        if entry.get("energy_level"):
            lines.append(f"  Evening energy: {'⚡' * entry['energy_level']}")
        if entry.get("microscope_obs"):
            lines.append(f"  Observation: _{entry['microscope_obs']}_")
        if entry.get("reflection_text"):
            lines.append(f"  Reflection: _{entry['reflection_text']}_")
    else:
        lines.append("_No check-in yet today._")

    lines.append(f"\n*Urges today:* {len(urges)}")
    if urges:
        gave_in = sum(1 for u in urges if u.get("gave_in"))
        lines.append(f"  Gave in: {gave_in} / {len(urges)}")

    if boosters:
        lines.append("\n*Boosters:*")
        if boosters.get("sleep_hours"):
            lines.append(f"  Sleep: {boosters['sleep_hours']}h")
        lines.append(f"  Exercise: {'✅' if boosters.get('exercise_done') else '—'}")
        lines.append(f"  Meditation: {boosters.get('meditation_minutes', 0)} min")
        lines.append(f"  Breathing: {'✅' if boosters.get('breathing_done') else '—'}")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


# ── /week ─────────────────────────────────────────────────────────────────────

async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    cycle = await db.get_active_cycle(user_id)
    if not cycle:
        await _no_cycle(update)
        return

    program = load_program()
    week = program.get(cycle["current_week"], {})
    microscope = week.get("microscope", {})
    experiment = week.get("experiment", {})

    lines = [
        f"*Week {cycle['current_week']}: {week.get('title', '')}*\n",
        f"_{week.get('theme', '')}_\n",
        f"*Under the Microscope*\n{microscope.get('text', '')}",
        f"\n*This Week's Experiment*\n{experiment.get('text', '')}",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


# ── /status ───────────────────────────────────────────────────────────────────

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    cycle = await db.get_active_cycle(user_id)
    if not cycle:
        await _no_cycle(update)
        return

    started = date.fromisoformat(cycle["started_at"])
    days_elapsed = (date.today() - started).days
    day_in_week = (days_elapsed % 7) + 1

    challenges = await db.get_challenges(cycle["id"])
    ch_lines = "\n".join(
        f"  {_type_label(c['challenge_type'])}: _{c['challenge_text']}_"
        for c in challenges
    ) or f"  _{cycle['challenge_text']}_"

    await update.message.reply_text(
        f"*Active Cycle*\n"
        f"*Challenges:*\n{ch_lines}\n"
        f"I Want: _{cycle['i_want_anchor']}_\n\n"
        f"Week {cycle['current_week']} of {config_max_week()} — Day {day_in_week} of 7\n"
        f"Started: {started.strftime('%B %d, %Y')}",
        parse_mode=ParseMode.MARKDOWN,
    )


# ── /setweek ──────────────────────────────────────────────────────────────────

async def cmd_setweek(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    cycle = await db.get_active_cycle(user_id)
    if not cycle:
        await _no_cycle(update)
        return

    if not context.args:
        await update.message.reply_text(
            f"Usage: `/setweek <1–{config_max_week()}>`\nExample: `/setweek 2`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    try:
        n = int(context.args[0])
    except ValueError:
        await update.message.reply_text(f"Please give a number between 1 and {config_max_week()}.")
        return

    if not 1 <= n <= config_max_week():
        await update.message.reply_text(f"Week must be between 1 and {config_max_week()}.")
        return

    old = cycle["current_week"]
    await db.advance_week(cycle["id"], n)

    program = load_program()
    week = program.get(n, {})
    direction = "⏩" if n > old else "⏪"
    await update.message.reply_text(
        f"{direction} Moved from Week {old} → *Week {n}: {week.get('title', '')}*\n\n"
        f"_{week.get('theme', '')}_",
        parse_mode=ParseMode.MARKDOWN,
    )


# ── /reset ────────────────────────────────────────────────────────────────────

async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    cycle = await db.get_active_cycle(user_id)
    if not cycle:
        await _no_cycle(update)
        return ConversationHandler.END

    kb = _kb([
        [("Yes, archive & restart", "reset:yes"), ("Cancel", "reset:no")],
    ])
    await update.message.reply_text(
        f"Archive current cycle (Week {cycle['current_week']}, "
        f"started {cycle['started_at']}) and start fresh?\n\n"
        "History is preserved — nothing is deleted.",
        reply_markup=kb,
    )
    return RESET_CONFIRM


async def reset_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if "no" in query.data:
        await query.edit_message_text("Reset cancelled.")
        return ConversationHandler.END

    user_id = update.effective_user.id
    cycle = await db.get_active_cycle(user_id)
    if cycle:
        await db.archive_cycle(cycle["id"])
    await query.edit_message_text(
        "Cycle archived. Use /start to begin a new one."
    )
    return ConversationHandler.END


async def reset_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Reset cancelled.")
    return ConversationHandler.END


# ── /history ──────────────────────────────────────────────────────────────────

async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    cycle = await db.get_active_cycle(user_id)
    if not cycle:
        await _no_cycle(update)
        return

    week_num = cycle["current_week"]
    if context.args:
        try:
            week_num = int(context.args[0])
        except ValueError:
            pass

    synthesis = await db.get_synthesis(cycle["id"], week_num)
    if not synthesis:
        await update.message.reply_text(
            f"No synthesis for Week {week_num} yet.\n"
            "Syntheses are generated Sunday evening after the week completes."
        )
        return

    gen_at = datetime.fromisoformat(synthesis["generated_at"]).strftime("%B %d")
    await update.message.reply_text(
        f"*Week {week_num} Synthesis* (generated {gen_at})\n\n"
        f"{synthesis['claude_response_text']}",
        parse_mode=ParseMode.MARKDOWN,
    )


# ── /review — per-challenge AI review ────────────────────────────────────────

async def cmd_review(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    import asyncio
    from .synthesis import run_challenge_review

    user_id = update.effective_user.id
    cycle = await db.get_active_cycle(user_id)
    if not cycle:
        await _no_cycle(update)
        return

    challenges = await db.get_challenges(cycle["id"])
    if not challenges:
        await update.message.reply_text(
            "No challenges set up yet. Add challenges at the web panel or use /start."
        )
        return

    if len(challenges) == 1:
        await update.message.reply_text("Generating review… ⏳")
        text = await run_challenge_review(user_id, cycle, challenges[0])
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
        return

    buttons = [
        [(f"{_type_label(c['challenge_type'])}: {c['challenge_text'][:50]}",
          f"review:ch:{c['id']}")]
        for c in challenges
    ]
    await update.message.reply_text(
        "Which challenge would you like a review on?",
        reply_markup=_kb(buttons),
    )


async def review_pick_challenge(update: Update,
                                 context: ContextTypes.DEFAULT_TYPE) -> None:
    import asyncio
    from .synthesis import run_challenge_review

    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    challenge_id = int(query.data.split(":")[-1])

    cycle = await db.get_active_cycle(user_id)
    if not cycle:
        await query.edit_message_text("No active cycle.")
        return

    challenges = await db.get_challenges(cycle["id"])
    challenge = next((c for c in challenges if c["id"] == challenge_id), None)
    if not challenge:
        await query.edit_message_text("Challenge not found.")
        return

    await query.edit_message_text("Generating review… ⏳")
    text = await run_challenge_review(user_id, cycle, challenge)
    await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN)


# ── Morning energy check (scheduler-driven) ──────────────────────────────────

async def _send_morning_energy_prompt(bot, chat_id: int) -> None:
    # chat_id kept for signature compat; sends go to the Willpower topic
    kb = _kb([[
        ("⚡", "morning:energy:1"), ("⚡⚡", "morning:energy:2"),
        ("⚡⚡⚡", "morning:energy:3"), ("⚡⚡⚡⚡", "morning:energy:4"),
        ("⚡⚡⚡⚡⚡", "morning:energy:5"),
    ]])
    from .config import send_kwargs
    await bot.send_message(
        text="Morning energy (1 = depleted, 5 = full)?",
        reply_markup=kb,
        **send_kwargs(),
    )


async def morning_energy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    level = int(query.data.split(":")[-1])
    cycle = await db.get_active_cycle(user_id)
    if cycle:
        await db.upsert_daily_entry(user_id, cycle["id"], cycle["current_week"],
                                    morning_energy=level)
    await query.edit_message_text(f"Morning energy: {'⚡' * level} — good luck today.")


# ── Evening check-in callbacks (scheduler-driven) ────────────────────────────
# State flows via user_data["checkin"] dict and user_data["awaiting"] for text.

async def _send_energy_prompt(context: ContextTypes.DEFAULT_TYPE,
                              chat_id: int) -> None:
    # chat_id kept for signature compat; sends go to the Willpower topic
    kb = _kb([[
        ("⚡", "checkin:energy:1"), ("⚡⚡", "checkin:energy:2"),
        ("⚡⚡⚡", "checkin:energy:3"), ("⚡⚡⚡⚡", "checkin:energy:4"),
        ("⚡⚡⚡⚡⚡", "checkin:energy:5"),
    ]])
    from .config import send_kwargs
    await context.bot.send_message(
        text="Evening check-in 🌙\n\nEnergy level today (1 = drained, 5 = full)?",
        reply_markup=kb,
        **send_kwargs(),
    )


async def _ask_next_challenge_adherence(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    challenges = context.user_data.get("checkin_challenges", [])
    idx = context.user_data.get("checkin_ch_idx", 0)
    ch = challenges[idx]
    label = _type_label(ch["challenge_type"])
    text = ch["challenge_text"][:80]
    kb = _kb([[
        ("✅ Yes", f"checkin:cha:{ch['id']}:yes"),
        ("〰️ Partial", f"checkin:cha:{ch['id']}:partial"),
        ("❌ No", f"checkin:cha:{ch['id']}:no"),
    ]])
    await query.edit_message_text(
        f"*{label}:* _{text}_\n\nHow did you do today?",
        reply_markup=kb,
        parse_mode=ParseMode.MARKDOWN,
    )


async def checkin_energy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    level = int(query.data.split(":")[-1])
    context.user_data.setdefault("checkin", {})["energy_level"] = level

    cycle = await db.get_active_cycle(user_id)
    if not cycle:
        return
    challenges = await db.get_challenges(cycle["id"])
    if challenges:
        context.user_data["checkin_challenges"] = challenges
        context.user_data["checkin_ch_idx"] = 0
        context.user_data["checkin_cycle_id"] = cycle["id"]
        await _ask_next_challenge_adherence(query, context)
    else:
        # No challenges yet — go straight to urge count
        kb = _kb([[
            ("0", "checkin:urges:0"), ("1", "checkin:urges:1"),
            ("2", "checkin:urges:2"), ("3", "checkin:urges:3"),
            ("4", "checkin:urges:4"), ("5+", "checkin:urges:5"),
        ]])
        await query.edit_message_text(
            f"Energy: {'⚡' * level}\n\nHow many urges did you notice today?",
            reply_markup=kb,
        )


async def checkin_challenge_adherence(update: Update,
                                       context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    parts = query.data.split(":")  # checkin:cha:<id>:<adherence>
    challenge_id = int(parts[2])
    adherence = parts[3]

    cycle_id = context.user_data.get("checkin_cycle_id")
    if cycle_id:
        await db.log_challenge_adherence(user_id, cycle_id, challenge_id, adherence)

    context.user_data["checkin_ch_idx"] = context.user_data.get("checkin_ch_idx", 0) + 1
    challenges = context.user_data.get("checkin_challenges", [])
    idx = context.user_data["checkin_ch_idx"]

    if idx < len(challenges):
        await _ask_next_challenge_adherence(query, context)
    else:
        # All challenges done — move to urge count
        kb = _kb([[
            ("0", "checkin:urges:0"), ("1", "checkin:urges:1"),
            ("2", "checkin:urges:2"), ("3", "checkin:urges:3"),
            ("4", "checkin:urges:4"), ("5+", "checkin:urges:5"),
        ]])
        await query.edit_message_text("How many urges did you notice today?", reply_markup=kb)


async def checkin_urges(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    count = int(query.data.split(":")[-1])
    context.user_data.setdefault("checkin", {})["urge_count"] = count
    # Ask about boosters based on current week's structured questions
    cycle = await db.get_active_cycle(user_id)
    if not cycle:
        return
    program = load_program()
    week = program.get(cycle["current_week"], {})
    sq_ids = [q["id"] for q in week.get("structured_questions", [])]
    # Check if this week tracks sleep
    if "sleep_hours" in sq_ids:
        await query.edit_message_text(
            "How many hours did you sleep last night?\n(Reply with a number, e.g. 7.5)"
        )
        context.user_data["awaiting"] = "sleep"
    elif "breathing_done" in sq_ids:
        kb = _kb([[("✅ Yes", "checkin:breathing:1"), ("❌ No", "checkin:breathing:0")]])
        await query.edit_message_text("Did you do the slow breathing exercise today?",
                                      reply_markup=kb)
    elif "exercise_done" in sq_ids:
        kb = _kb([[("✅ Yes", "checkin:exercise:1"), ("❌ No", "checkin:exercise:0")]])
        await query.edit_message_text("Did you exercise today?", reply_markup=kb)
    else:
        await _ask_observation(query, context, user_id)


async def checkin_breathing(update: Update,
                             context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    val = int(query.data.split(":")[-1])
    context.user_data.setdefault("checkin", {})["breathing_done"] = val
    cycle = await db.get_active_cycle(user_id)
    if not cycle:
        return
    program = load_program()
    week = program.get(cycle["current_week"], {})
    sq_ids = [q["id"] for q in week.get("structured_questions", [])]
    if "exercise_done" in sq_ids:
        kb = _kb([[("✅ Yes", "checkin:exercise:1"), ("❌ No", "checkin:exercise:0")]])
        await query.edit_message_text("Did you exercise today?", reply_markup=kb)
    else:
        await _ask_observation(query, context, user_id)


async def checkin_exercise(update: Update,
                            context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    val = int(query.data.split(":")[-1])
    context.user_data.setdefault("checkin", {})["exercise_done"] = val
    await _ask_observation(update.callback_query, context, user_id)


async def _ask_observation(query, context: ContextTypes.DEFAULT_TYPE,
                            user_id: int) -> None:
    cycle = await db.get_active_cycle(user_id)
    if not cycle:
        return
    program = load_program()
    week = program.get(cycle["current_week"], {})
    microscope_prompt = week.get("microscope", {}).get("daily_prompt", "")
    await query.edit_message_text(
        f"Microscope observation:\n_{microscope_prompt}_\n\n"
        "(Free text — or reply *skip*)",
        parse_mode=ParseMode.MARKDOWN,
    )
    context.user_data["awaiting"] = "obs"


async def checkin_free_text(update: Update,
                             context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles free-text steps: sleep hours, microscope obs, evening reflection."""
    awaiting = context.user_data.get("awaiting")
    if not awaiting:
        return

    user_id = update.effective_user.id
    text = update.message.text.strip()
    skipped = text.lower() == "skip"
    cycle = await db.get_active_cycle(user_id)
    if not cycle:
        return

    if awaiting == "sleep":
        try:
            hours = float(text)
        except ValueError:
            await update.message.reply_text("Please enter a number (e.g. 7 or 7.5).")
            return
        context.user_data.setdefault("checkin", {})["sleep_hours"] = hours
        context.user_data.pop("awaiting")
        program = load_program()
        week = program.get(cycle["current_week"], {})
        sq_ids = [q["id"] for q in week.get("structured_questions", [])]
        if "breathing_done" in sq_ids:
            kb = _kb([[("✅ Yes", "checkin:breathing:1"),
                       ("❌ No", "checkin:breathing:0")]])
            await update.message.reply_text("Did you do the slow breathing exercise today?",
                                            reply_markup=kb)
        elif "exercise_done" in sq_ids:
            kb = _kb([[("✅ Yes", "checkin:exercise:1"),
                       ("❌ No", "checkin:exercise:0")]])
            await update.message.reply_text("Did you exercise today?", reply_markup=kb)
        else:
            await _ask_observation_msg(update, context, user_id)
        return

    if awaiting == "obs":
        context.user_data.setdefault("checkin", {})["microscope_obs"] = (
            None if skipped else text
        )
        context.user_data["awaiting"] = "reflect"
        program = load_program()
        week = program.get(cycle["current_week"], {})
        exp_prompt = week.get("experiment", {}).get("daily_prompt", "")
        await update.message.reply_text(
            f"Evening reflection:\n_{exp_prompt}_\n\n(Free text — or reply *skip*)",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if awaiting == "reflect":
        context.user_data.setdefault("checkin", {})["reflection_text"] = (
            None if skipped else text
        )
        context.user_data.pop("awaiting")
        await _save_checkin(update, context, user_id, cycle)


async def _ask_observation_msg(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                user_id: int) -> None:
    cycle = await db.get_active_cycle(user_id)
    if not cycle:
        return
    program = load_program()
    week = program.get(cycle["current_week"], {})
    microscope_prompt = week.get("microscope", {}).get("daily_prompt", "")
    await update.message.reply_text(
        f"Microscope observation:\n_{microscope_prompt}_\n\n(Free text — or reply *skip*)",
        parse_mode=ParseMode.MARKDOWN,
    )
    context.user_data["awaiting"] = "obs"


async def _save_checkin(update: Update, context: ContextTypes.DEFAULT_TYPE,
                         user_id: int, cycle: dict) -> None:
    data = context.user_data.pop("checkin", {})
    context.user_data.pop("checkin_challenges", None)
    context.user_data.pop("checkin_ch_idx", None)
    context.user_data.pop("checkin_cycle_id", None)
    week_num = cycle["current_week"]

    entry_fields = {k: v for k, v in data.items()
                    if k in ("energy_level", "urge_count",
                             "microscope_obs", "reflection_text")}
    booster_fields = {k: v for k, v in data.items()
                      if k in ("sleep_hours", "exercise_done",
                               "meditation_minutes", "breathing_done")}

    if entry_fields:
        await db.upsert_daily_entry(user_id, cycle["id"], week_num, **entry_fields)
    if booster_fields:
        await db.upsert_boosters(user_id, cycle["id"], **booster_fields)

    await update.message.reply_text(
        "Check-in saved ✅\nUse /today to see your full day summary."
    )


# ── /tips ─────────────────────────────────────────────────────────────────────

_TIPS_SYSTEM = (
    "You are a concise, practical coach for Kelly McGonigal's Willpower Instinct program. "
    "Give 3–5 concrete, actionable tips tailored to the week's specific theme and experiment. "
    "Be specific to the chapter content — not generic advice. Under 250 words. "
    "Format as a short numbered list."
)


async def cmd_tips(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    import asyncio
    from .synthesis import _client
    from .config import SYNTHESIS_MODEL

    user_id = update.effective_user.id
    cycle = await db.get_active_cycle(user_id)
    if not cycle:
        await _no_cycle(update)
        return

    program = load_program()
    week_num = cycle["current_week"]
    week = program.get(week_num, {})
    if not week or str(week.get("title", "")).startswith("STUB"):
        await update.message.reply_text(
            f"Week {week_num} content isn't filled in yet. Edit program.yaml to add it."
        )
        return

    await update.message.reply_text("Generating tips… ⏳")

    user_msg = (
        f"I'm on Week {week_num} of The Willpower Instinct: '{week.get('title', '')}'.\n\n"
        f"Theme: {week.get('theme', '')}\n\n"
        f"Under the Microscope: {week.get('microscope', {}).get('text', '')}\n\n"
        f"This week's experiment: {week.get('experiment', {}).get('text', '')}\n\n"
        f"My challenge: {cycle['challenge_text']}\n"
        f"My I Want anchor: {cycle['i_want_anchor']}\n\n"
        "Give me 3–5 practical tips to get the most out of this week."
    )

    response = await asyncio.to_thread(
        _client.messages.create,
        model=SYNTHESIS_MODEL,
        max_tokens=400,
        system=_TIPS_SYSTEM,
        messages=[{"role": "user", "content": user_msg}],
    )
    await update.message.reply_text(
        f"*Week {week_num} Tips*\n\n{response.content[0].text}",
        parse_mode=ParseMode.MARKDOWN,
    )


# ── /help ─────────────────────────────────────────────────────────────────────

_HELP_TEXT = """\
*Willpower Instinct Bot — Commands*

*Setup*
/start — Begin a new 10-week cycle
/reset — Archive the current cycle and start fresh
/setweek N — Jump to week N (1–14), forward or backward

*Daily*
/log — Log an urge (trigger, intensity, gave in?)
/today — See today's entry, urges, and boosters

*Weekly*
/week — This week's theme, microscope exercise & experiment
/tips — AI-generated tips for the current week
/history [N] — Show Week N synthesis (default: current week)
/review — AI review of a specific challenge's data

*Info*
/status — Cycle overview (week, day, challenge)
/help — Show this message
"""


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(_HELP_TEXT, parse_mode=ParseMode.MARKDOWN)


# ── handler registration helper ───────────────────────────────────────────────

def register_handlers(app) -> None:
    start_conv = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            CHOOSE_TYPE:       [CallbackQueryHandler(start_choose_type,
                                                     pattern=r"^start:type:")],
            ENTER_CHALLENGE:   [MessageHandler(filters.TEXT & ~filters.COMMAND,
                                               start_enter_challenge)],
            ENTER_WANT:        [MessageHandler(filters.TEXT & ~filters.COMMAND,
                                               start_enter_want)],
            CONFIRM:           [CallbackQueryHandler(start_confirm,
                                                     pattern=r"^start:confirm:")],
        },
        fallbacks=[CommandHandler("cancel", start_cancel)],
    )

    log_conv = ConversationHandler(
        entry_points=[CommandHandler("log", cmd_log)],
        states={
            LOG_WEEK:      [CallbackQueryHandler(log_pick_week,      pattern=r"^log:week:")],
            LOG_PICK:      [CallbackQueryHandler(log_pick_challenge, pattern=r"^log:pick:")],
            LOG_OUTCOME:   [CallbackQueryHandler(log_outcome,        pattern=r"^log:outcome:")],
            LOG_WEEK1:     [CallbackQueryHandler(log_week1,          pattern=r"^log:w1want:")],
            LOG_WEEK2:     [CallbackQueryHandler(log_week2,          pattern=r"^log:w2breath:")],
            LOG_WEEK3:     [CallbackQueryHandler(log_week3,          pattern=r"^log:w3recovery:")],
            LOG_TRIGGER:   [MessageHandler(filters.TEXT & ~filters.COMMAND, log_trigger)],
            LOG_INTENSITY: [CallbackQueryHandler(log_intensity,      pattern=r"^log:intensity:")],
            LOG_NOTES:     [MessageHandler(filters.TEXT & ~filters.COMMAND, log_notes)],
        },
        fallbacks=[CommandHandler("cancel", log_cancel)],
    )

    reset_conv = ConversationHandler(
        entry_points=[CommandHandler("reset", cmd_reset)],
        states={
            RESET_CONFIRM: [CallbackQueryHandler(reset_confirm, pattern=r"^reset:")],
        },
        fallbacks=[CommandHandler("cancel", reset_cancel)],
    )

    app.add_handler(start_conv)
    app.add_handler(log_conv)
    app.add_handler(reset_conv)

    app.add_handler(CommandHandler("today",   cmd_today))
    app.add_handler(CommandHandler("week",    cmd_week))
    app.add_handler(CommandHandler("tips",    cmd_tips))
    app.add_handler(CommandHandler("status",  cmd_status))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("setweek", cmd_setweek))
    app.add_handler(CommandHandler("review",  cmd_review))
    app.add_handler(CommandHandler("help",    cmd_help))

    # Morning energy
    app.add_handler(CallbackQueryHandler(morning_energy,             pattern=r"^morning:energy:"))

    # Evening check-in callbacks
    app.add_handler(CallbackQueryHandler(checkin_energy,             pattern=r"^checkin:energy:"))
    app.add_handler(CallbackQueryHandler(checkin_challenge_adherence,pattern=r"^checkin:cha:"))
    app.add_handler(CallbackQueryHandler(checkin_urges,              pattern=r"^checkin:urges:"))
    app.add_handler(CallbackQueryHandler(checkin_breathing,          pattern=r"^checkin:breathing:"))
    app.add_handler(CallbackQueryHandler(checkin_exercise,           pattern=r"^checkin:exercise:"))

    # Review pick
    app.add_handler(CallbackQueryHandler(review_pick_challenge,      pattern=r"^review:ch:"))

    # Free-text replies for check-in and reflection (lower priority — runs after convs)
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            checkin_free_text,
        ),
        group=1,
    )
