"""
Entry point. Wires the Application, registers handlers + daily jobs,
rehydrates today's jobs from DB, then starts long-polling.
"""
import logging
import sys

from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

from . import config
from . import db
from .handlers import (
    cmd_start,
    cmd_today,
    cmd_skip,
    cmd_snooze,
    cmd_silence_today,
    cmd_done,
    cmd_help,
    cmd_lesson,
    cmd_lessons,
    cmd_weekly,
    cmd_trends,
    cmd_todo,
    cmd_at,
    cmd_begin,
    cmd_complete_task,
    cmd_pause_task,
    handle_text,
    handle_callback,
)
from .jobs import schedule_morning, schedule_evening, rehydrate_jobs, check_daily_jobs_scheduled

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


# No set_my_commands() here: this bot shares one bot account (PING_BOT_ID)
# with the other todo_list bots in the same chat, and Telegram's command-menu
# scoping only goes down to chat level, not per-topic — registering a menu
# would overwrite the autocomplete list for every topic. /help still works.


def build_app() -> Application:
    db.init_db()

    app = (
        Application.builder()
        .token(config.BOT_TOKEN)
        .build()
    )

    # --- commands ---
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("skip", cmd_skip))
    app.add_handler(CommandHandler("snooze", cmd_snooze))
    app.add_handler(CommandHandler("silence_today", cmd_silence_today))
    app.add_handler(CommandHandler("done", cmd_done))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("lesson", cmd_lesson))
    app.add_handler(CommandHandler("lessons", cmd_lessons))
    app.add_handler(CommandHandler("weekly", cmd_weekly))
    app.add_handler(CommandHandler("trends", cmd_trends))
    app.add_handler(CommandHandler("todo", cmd_todo))
    app.add_handler(CommandHandler("at", cmd_at))
    app.add_handler(CommandHandler("begin", cmd_begin))
    app.add_handler(CommandHandler("complete_task", cmd_complete_task))
    app.add_handler(CommandHandler("pause_task", cmd_pause_task))

    # --- free text ---
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # --- inline keyboard callbacks ---
    app.add_handler(CallbackQueryHandler(handle_callback))

    # --- error handler so job failures appear in logs ---
    async def _error_handler(update, context) -> None:
        logger.error("Unhandled exception", exc_info=context.error)
    app.add_error_handler(_error_handler)

    # --- daily prompts (self-rescheduling run_once, avoids APScheduler TZ issues) ---
    schedule_morning(app)
    schedule_evening(app)

    # --- hourly self-heal: catch the morning/evening job silently vanishing ---
    app.job_queue.run_repeating(check_daily_jobs_scheduled, interval=3600, first=3600, name="daily_jobs_healthcheck")

    # --- rehydrate today's one-shot jobs after a restart ---
    rehydrate_jobs(app)
    logger.info("Job rehydration complete.")

    return app


async def run_fed(raw_queue) -> None:
    """Run fed by an external queue.Queue of raw Telegram update dicts,
    instead of polling getUpdates itself. Used by todo_list/run_bots.py —
    the shared PING_BOT_ID token allows only one poller, which routes this
    bot's topic (ADHD_THREAD_ID) here. Application.start() also starts the
    PTB job_queue, so the self-rescheduling daily prompts keep working."""
    import asyncio
    from telegram import Update

    app = build_app()
    async with app:
        await app.start()
        logger.info("ADHD bot started (fed mode)")
        loop = asyncio.get_event_loop()
        while True:
            raw = await loop.run_in_executor(None, raw_queue.get)
            update = Update.de_json(raw, app.bot)
            await app.update_queue.put(update)


def main() -> None:
    app = build_app()
    logger.info("Bot starting (standalone polling)…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
