import asyncio
import functools
import json
import logging
import os
import subprocess
import traceback
from datetime import datetime, timedelta
from pathlib import Path

from telegram import BotCommand, Update
from telegram.ext import (
    Application, CommandHandler, ContextTypes, MessageHandler, MessageReactionHandler, filters,
)

import store
import inspiration as insp

logger = logging.getLogger(__name__)

_app: Application | None = None

# Optional channel mode: if set, Franklin listens/sends to this channel topic
FRANKLIN_CHANNEL_ID = int(os.environ.get("FRANKLIN_CHANNEL_ID", "0") or "0")
FRANKLIN_THREAD_ID  = int(os.environ.get("FRANKLIN_THREAD_ID",  "0") or "0")

WINDOW_END = 23  # matches pinger/accountability's default day-end for "pause rest of day"
PAUSE_FILE = Path(__file__).parent / "franklin_pause_state.json"


def is_paused() -> bool:
    if not PAUSE_FILE.exists():
        return False
    state = json.loads(PAUSE_FILE.read_text())
    resume_at = state.get("resume_at")
    if resume_at and datetime.fromisoformat(resume_at) <= datetime.now():
        PAUSE_FILE.unlink(missing_ok=True)
        return False
    return True


def _write_pause(resume_at: datetime) -> None:
    PAUSE_FILE.write_text(json.dumps({
        "paused_at": datetime.now().isoformat(),
        "resume_at": resume_at.isoformat(),
    }, indent=2))


def _is_authorized(update: Update) -> bool:
    """Allow messages from the configured channel topic or the registered owner DM."""
    if FRANKLIN_CHANNEL_ID:
        if update.effective_chat.id != FRANKLIN_CHANNEL_ID:
            return False
        if FRANKLIN_THREAD_ID:
            thread = getattr(update.effective_message, "message_thread_id", None)
            return thread == FRANKLIN_THREAD_ID
        return True
    allowed_id = store.get_owner_chat_id()
    return allowed_id is not None and update.effective_chat.id == allowed_id


# ---------------------------------------------------------------------------
# Decorators
# ---------------------------------------------------------------------------

def _only_owner(handler):
    @functools.wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not _is_authorized(update):
            if store.get_owner_chat_id() is None and not FRANKLIN_CHANNEL_ID:
                await update.message.reply_text("Send /start to register as owner first.")
            else:
                logger.warning("Blocked message from chat %s", update.effective_chat.id)
            return
        return await handler(update, context)
    return wrapper


def _safe(handler):
    @functools.wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            return await handler(update, context)
        except Exception:
            logger.error("Handler error:\n%s", traceback.format_exc())
            if update.effective_message:
                await update.effective_message.reply_text("Something went wrong, check logs.")
    return wrapper


def _gate(handler):
    return _only_owner(_safe(handler))


# ---------------------------------------------------------------------------
# Shared message builders
# ---------------------------------------------------------------------------

def build_morning_message() -> str:
    virtue = store.current_focus_virtue()
    blurb = insp.get_morning_text(virtue["id"])
    lines = [
        f"Good morning! Focus virtue: *{virtue['name']}*",
        f"_{virtue['precept']}_",
        "",
        blurb,
    ]
    open_todos = store.get_open_todos(virtue["id"])
    lines.append("")
    if open_todos:
        lines.append(f"Open todos for {virtue['name']}:")
        for t in open_todos:
            lines.append(f"• {t['id']} — {t['text']}")
    else:
        lines.append(f"No open todos for {virtue['name']}. Add some with /todo <text>.")
    return "\n".join(lines)


def build_nudge_message() -> str:
    virtue = store.current_focus_virtue()
    open_todos = store.get_open_todos(virtue["id"])
    if open_todos:
        first = open_todos[0]["text"]
        return f"Focus: {virtue['name']}. {len(open_todos)} open — next: {first}."
    return f"Focus: {virtue['name']}."


def build_weekly_summary() -> str:
    from datetime import date, timedelta
    config = store.load_config()
    virtue = store.current_focus_virtue()

    today = date.today()
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)
    week_marks = store.get_marks(monday.isoformat(), sunday.isoformat())

    virtues = config["virtues"]
    days = [(monday + timedelta(days=i)) for i in range(7)]
    day_labels = [d.strftime("%a") for d in days]

    # Monospace grid
    col_w = 3
    name_w = max(len(v["name"]) for v in virtues)
    header = " " * name_w + "  " + "  ".join(f"{lbl:>{col_w}}" for lbl in day_labels)
    rows = [header]
    for v in virtues:
        cells = []
        for d in days:
            count = week_marks.get(d.isoformat(), {}).get(v["id"], 0)
            cells.append(f"{count if count else '·':>{col_w}}")
        marker = "▶" if v["id"] == virtue["id"] else " "
        rows.append(f"{marker}{v['name']:<{name_w}}  " + "  ".join(cells))
    grid = "\n".join(rows)

    # Todo stats
    data = store.load_data()
    week_start_iso = monday.isoformat()
    week_end_iso = sunday.isoformat()
    week_todos = [
        t for t in data.get("todos", [])
        if week_start_iso <= t["created"][:10] <= week_end_iso
    ]
    opened = len(week_todos)
    closed = sum(1 for t in week_todos if t["status"] == "done")
    cancelled = sum(1 for t in week_todos if t["status"] == "cancelled")
    still_open = sum(1 for t in week_todos if t["status"] == "open")

    week_notes = store.get_notes(since=f"{week_start_iso}T00:00:00+00:00")
    recent_notes = sorted(week_notes, key=lambda n: n["ts"], reverse=True)[:2]

    lines = [
        f"Week of {monday.isoformat()} — focus: {virtue['name']}",
        "",
        f"```\n{grid}\n```",
        "",
        f"Todos: {opened} opened, {closed} closed, {cancelled} cancelled, {still_open} still open.",
        f"Notes this week: {len(week_notes)}",
    ]
    if recent_notes:
        for n in recent_notes:
            v_name = next((v["name"] for v in virtues if v["id"] == n.get("virtue")), "General")
            lines.append(f"  • [{v_name} {n['ts'][:10]}] {n['text']}")

    # Claude synthesis (optional, suppress errors)
    try:
        import coach
        synthesis = coach.reflect_on_virtue(virtue["id"], weekly_recap=True)
        lines += ["", synthesis]
    except Exception:
        pass

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

_HELP_TEXT = (
    "🏛 *Franklin* — virtue tracker & daily planner\n\n"
    "/today — morning prompt + focus virtue\n"
    "/focus — current focus virtue\n"
    "/virtues — list all 13 virtues\n"
    "/todo `<text>` — add a todo\n"
    "/done `<id>` — mark todo done\n"
    "/cancel `<id>` — cancel a todo\n"
    "/note `<text>` — add a note\n"
    "/coach — reflection from Claude\n"
    "/summary — weekly recap\n"
    "/web — open the evening review form\n"
    "/pause [hours] — pause morning/nudge prompts\n"
    "/resume — end a pause early\n"
    "/status — show pause state\n"
    "/help — show this message"
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    existing = store.get_owner_chat_id()
    if not FRANKLIN_CHANNEL_ID:
        if existing is None:
            store.set_owner_chat_id(chat_id)
            logger.info("Owner registered: chat_id=%s", chat_id)
        elif existing != chat_id:
            logger.warning("Ignoring /start from unknown chat %s", chat_id)
            return
    await update.message.reply_text("Franklin virtue tracker online.\n\n" + _HELP_TEXT,
                                    parse_mode="Markdown")


@_gate
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(_HELP_TEXT, parse_mode="Markdown")


@_gate
async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = build_morning_message()
    await update.message.reply_text(msg, parse_mode="Markdown")


@_gate
async def cmd_focus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    virtue = store.current_focus_virtue()
    await update.message.reply_text(
        f"*{virtue['name']}* — Week {virtue['week_number']} of 13\n_{virtue['precept']}_",
        parse_mode="Markdown",
    )


@_gate
async def cmd_virtues(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config = store.load_config()
    lines = [f"*{v['name']}* — {v['precept']}" for v in config["virtues"]]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@_gate
async def cmd_todo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args).strip()
    if not text:
        await update.message.reply_text("Usage: /todo <text>")
        return
    virtue = store.current_focus_virtue()
    todo_id = store.add_todo(virtue["id"], text)
    await update.message.reply_text(f"Added {todo_id}: {text}")


@_gate
async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /done <id>")
        return
    todo_id = context.args[0]
    try:
        store.set_todo_status(todo_id, "done")
        await update.message.reply_text(f"✓ {todo_id} done")
    except KeyError:
        await update.message.reply_text(f"No todo found with id {todo_id}.")


@_gate
async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /cancel <id>")
        return
    todo_id = context.args[0]
    try:
        store.set_todo_status(todo_id, "cancelled")
        await update.message.reply_text(f"✗ {todo_id} cancelled")
    except KeyError:
        await update.message.reply_text(f"No todo found with id {todo_id}.")


@_gate
async def cmd_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args).strip()
    if not text:
        await update.message.reply_text("Usage: /note <text>")
        return
    virtue = store.current_focus_virtue()
    note_id = store.add_note(virtue["id"], text)
    await update.message.reply_text(f"Noted {note_id}")


@_gate
async def cmd_coach(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Thinking…")
    virtue = store.current_focus_virtue()

    async def _run():
        try:
            import coach
            reflection = await asyncio.get_event_loop().run_in_executor(
                None, coach.reflect_on_virtue, virtue["id"]
            )
            await send_message(reflection)
        except Exception:
            await send_message("Couldn't reach the coach right now — try again later.")

    asyncio.create_task(_run())


@_gate
async def cmd_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = build_weekly_summary()
    await update.message.reply_text(msg, parse_mode="Markdown")


@_gate
async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now()
    if context.args:
        try:
            hours = float(context.args[0])
            resume_at = now + timedelta(hours=hours)
            _write_pause(resume_at)
            await update.message.reply_text(
                f"Paused for {hours:.4g}h (until {resume_at.strftime('%H:%M')}). /resume to end early."
            )
        except ValueError:
            await update.message.reply_text("Usage: /pause [hours] — e.g. /pause 2")
    else:
        resume_at = now.replace(hour=WINDOW_END, minute=0, second=0, microsecond=0)
        if now >= resume_at:
            resume_at += timedelta(days=1)
        _write_pause(resume_at)
        await update.message.reply_text(f"Paused for the rest of the day (until {WINDOW_END}:00).")


@_gate
async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_paused():
        PAUSE_FILE.unlink(missing_ok=True)
        await update.message.reply_text("Resumed! Prompts will continue.")
    else:
        await update.message.reply_text("Not currently paused.")


@_gate
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_paused():
        state = json.loads(PAUSE_FILE.read_text())
        await update.message.reply_text(
            f"Paused since {state['paused_at'][:16]}\nResumes at: {state.get('resume_at', '?')[:16]}"
        )
    else:
        await update.message.reply_text("Running normally.")


@_gate
async def cmd_web(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import web
    web.start_server()
    host = _get_tailscale_host()
    if host is None:
        await update.message.reply_text(
            "Could not determine Tailscale IP. Set TAILSCALE_HOST in .env or ensure "
            "'tailscale ip --4' works."
        )
        return
    port = int(os.environ.get("WEB_PORT", "8765"))
    await update.message.reply_text(f"http://{host}:{port}")


@_gate
async def cmd_debug_fire(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not os.environ.get("DEBUG_JOBS"):
        return
    if not context.args:
        await update.message.reply_text("Usage: /debug_fire <job>")
        return
    job_name = context.args[0]
    import scheduler as sched
    fn = sched.JOB_FUNCTIONS.get(job_name)
    if fn is None:
        await update.message.reply_text(f"Unknown job: {job_name}. Available: {list(sched.JOB_FUNCTIONS)}")
        return
    await fn()
    await update.message.reply_text(f"Fired job: {job_name}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_tailscale_host() -> str | None:
    host = os.environ.get("TAILSCALE_HOST")
    if host:
        return host.strip()
    try:
        result = subprocess.run(
            ["tailscale", "ip", "--4"],
            capture_output=True, text=True, timeout=3
        )
        ip = result.stdout.strip().splitlines()[0].strip()
        if ip:
            return ip
    except Exception:
        pass
    return None


async def send_message(text: str) -> "int | None":
    """Returns the sent message's message_id (or None if not sent), so
    callers — e.g. job_nudge — can record which physical message an
    advice/affirmation item became, for later reaction/reply tracking."""
    global _app
    if _app is None:
        logger.error("send_message called before app initialized")
        return None
    if FRANKLIN_CHANNEL_ID:
        kwargs = {"chat_id": FRANKLIN_CHANNEL_ID, "text": text, "parse_mode": "Markdown"}
        if FRANKLIN_THREAD_ID:
            kwargs["message_thread_id"] = FRANKLIN_THREAD_ID
        msg = await _app.bot.send_message(**kwargs)
        return msg.message_id
    chat_id = store.get_owner_chat_id()
    if chat_id is None:
        logger.warning("send_to_owner: no owner registered yet")
        return None
    msg = await _app.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
    return msg.message_id


# ---------------------------------------------------------------------------
# Advice feedback: 👍/👎 reactions and reply notes on advice/affirmation messages
# ---------------------------------------------------------------------------

async def on_advice_reaction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reaction = update.message_reaction
    if reaction is None:
        return
    if FRANKLIN_CHANNEL_ID and reaction.chat.id != FRANKLIN_CHANNEL_ID:
        return

    import advice_store
    found = advice_store.find_by_message_id(reaction.message_id)
    if found is None:
        return
    _, item = found

    old_emojis = {r.emoji for r in reaction.old_reaction if getattr(r, "emoji", None)}
    new_emojis = {r.emoji for r in reaction.new_reaction if getattr(r, "emoji", None)}
    added = new_emojis - old_emojis

    if "👍" in added:
        advice_store.append_good_example(item["virtue_name"], item["text"])
    elif "👎" in added:
        advice_store.append_bad_example(item["virtue_name"], item["text"])


async def on_advice_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        return
    msg = update.effective_message
    if not msg or not msg.reply_to_message or not msg.text:
        return

    import advice_store
    found = advice_store.find_by_message_id(msg.reply_to_message.message_id)
    if found is None:
        return
    _, item = found
    advice_store.append_note(msg.text.strip(), item["text"])


async def _post_init(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("today",    "Morning prompt + focus virtue"),
        BotCommand("focus",    "Current focus virtue"),
        BotCommand("virtues",  "List all 13 virtues"),
        BotCommand("todo",     "Add a todo: /todo finish report"),
        BotCommand("done",     "Mark todo done: /done 3"),
        BotCommand("cancel",   "Cancel a todo: /cancel 3"),
        BotCommand("note",     "Add a note"),
        BotCommand("coach",    "Reflection from Claude"),
        BotCommand("summary",  "Weekly recap"),
        BotCommand("web",      "Open the evening review form"),
        BotCommand("pause",    "Pause morning/nudge prompts"),
        BotCommand("resume",   "End a pause early"),
        BotCommand("status",   "Show pause state"),
        BotCommand("help",     "Show all commands"),
    ])


def build_application() -> Application:
    global _app
    token = os.environ["PING_BOT_ID"]
    _app = Application.builder().token(token).post_init(_post_init).build()

    _app.add_handler(CommandHandler("start",   cmd_start))
    _app.add_handler(CommandHandler("help",    cmd_help))
    _app.add_handler(CommandHandler("today",   cmd_today))
    _app.add_handler(CommandHandler("focus",   cmd_focus))
    _app.add_handler(CommandHandler("virtues", cmd_virtues))
    _app.add_handler(CommandHandler("todo",    cmd_todo))
    _app.add_handler(CommandHandler("done",    cmd_done))
    _app.add_handler(CommandHandler("cancel",  cmd_cancel))
    _app.add_handler(CommandHandler("note",    cmd_note))
    _app.add_handler(CommandHandler("coach",   cmd_coach))
    _app.add_handler(CommandHandler("summary", cmd_summary))
    _app.add_handler(CommandHandler("web",     cmd_web))
    _app.add_handler(CommandHandler("pause",   cmd_pause))
    _app.add_handler(CommandHandler("resume",  cmd_resume))
    _app.add_handler(CommandHandler("status",  cmd_status))

    _app.add_handler(MessageReactionHandler(on_advice_reaction))
    _app.add_handler(MessageHandler(filters.TEXT & filters.REPLY & ~filters.COMMAND, on_advice_reply))

    if os.environ.get("DEBUG_JOBS"):
        _app.add_handler(CommandHandler("debug_fire", cmd_debug_fire))

    return _app
