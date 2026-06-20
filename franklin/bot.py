import asyncio
import functools
import logging
import os
import subprocess
import traceback

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

import store
import inspiration as insp

logger = logging.getLogger(__name__)

_app: Application | None = None


# ---------------------------------------------------------------------------
# Decorators
# ---------------------------------------------------------------------------

def _only_owner(handler):
    @functools.wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        allowed_id = store.get_owner_chat_id()
        if allowed_id is None:
            await update.message.reply_text("Send /start to register as owner first.")
            return
        if update.effective_chat.id != allowed_id:
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

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    existing = store.get_owner_chat_id()
    if existing is None:
        store.set_owner_chat_id(chat_id)
        logger.info("Owner registered: chat_id=%s", chat_id)
    elif existing != chat_id:
        logger.warning("Ignoring /start from unknown chat %s", chat_id)
        return
    await update.message.reply_text(
        "Franklin virtue tracker online.\n\n"
        "Commands:\n"
        "/today — morning prompt\n"
        "/focus — current focus virtue\n"
        "/virtues — list all 13\n"
        "/todo <text> — add a todo\n"
        "/done <id> — mark todo done\n"
        "/cancel <id> — cancel a todo\n"
        "/note <text> — add a note\n"
        "/coach — get a reflection from Claude\n"
        "/summary — weekly recap\n"
        "/web — start the evening form"
    )


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


async def send_message(text: str) -> None:
    global _app
    if _app is None:
        logger.error("send_message called before app initialized")
        return
    chat_id = store.get_owner_chat_id()
    if chat_id is None:
        logger.warning("send_to_owner: no owner registered yet")
        return
    await _app.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")


def build_application() -> Application:
    global _app
    token = os.environ["franklin_3149987_bot"]
    _app = Application.builder().token(token).build()

    _app.add_handler(CommandHandler("start", cmd_start))
    _app.add_handler(CommandHandler("today", cmd_today))
    _app.add_handler(CommandHandler("focus", cmd_focus))
    _app.add_handler(CommandHandler("virtues", cmd_virtues))
    _app.add_handler(CommandHandler("todo", cmd_todo))
    _app.add_handler(CommandHandler("done", cmd_done))
    _app.add_handler(CommandHandler("cancel", cmd_cancel))
    _app.add_handler(CommandHandler("note", cmd_note))
    _app.add_handler(CommandHandler("coach", cmd_coach))
    _app.add_handler(CommandHandler("summary", cmd_summary))
    _app.add_handler(CommandHandler("web", cmd_web))

    if os.environ.get("DEBUG_JOBS"):
        _app.add_handler(CommandHandler("debug_fire", cmd_debug_fire))

    return _app
