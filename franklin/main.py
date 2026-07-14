import asyncio
import logging
import os
import signal
import sys

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

REQUIRED_KEYS = ["PING_BOT_ID", "ANTHROPIC_API_KEY"]


def validate_env():
    missing = [k for k in REQUIRED_KEYS if not os.environ.get(k)]
    if missing:
        print(f"ERROR: Missing required env vars: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)


async def main():
    validate_env()

    import store
    import bot
    import scheduler as sched

    virtue = store.current_focus_virtue()
    logger.info("Config loaded. Focus virtue: %s (week %d)", virtue["name"], virtue["week_number"])

    application = bot.build_application()
    scheduler = sched.build_scheduler()

    loop = asyncio.get_event_loop()

    def _shutdown(sig, frame):
        logger.info("Received %s — shutting down", sig)
        scheduler.shutdown(wait=False)
        loop.call_soon_threadsafe(loop.stop)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    scheduler.start()
    logger.info("Scheduler started with %d jobs", len(scheduler.get_jobs()))

    async with application:
        await application.start()
        await application.updater.start_polling(drop_pending_updates=True)
        logger.info("Bot polling started. Press Ctrl+C to stop.")

        stop_event = asyncio.Event()

        def _set_stop():
            stop_event.set()

        loop.add_signal_handler(signal.SIGINT, _set_stop)
        loop.add_signal_handler(signal.SIGTERM, _set_stop)

        await stop_event.wait()

        await application.updater.stop()
        await application.stop()
        scheduler.shutdown(wait=False)
        logger.info("Shutdown complete.")


async def run_fed(raw_queue) -> None:
    """Run Franklin fed by an external queue.Queue of raw Telegram update
    dicts, instead of polling getUpdates itself. Used by
    todo_list/run_bots.py when Franklin shares a bot token (PING_BOT_ID)
    with pinger.py/accountability_bot.py — only one process may hold the
    getUpdates long-poll for a given token, so a single shared poller feeds
    all three via queues, routed by Telegram topic (message_thread_id)."""
    validate_env()

    import store
    import bot
    import scheduler as sched
    from telegram import Update

    virtue = store.current_focus_virtue()
    logger.info("Config loaded. Focus virtue: %s (week %d)", virtue["name"], virtue["week_number"])

    application = bot.build_application()
    scheduler = sched.build_scheduler()

    async with application:
        await application.start()
        scheduler.start()
        logger.info("Franklin started (fed mode). Scheduler jobs: %d", len(scheduler.get_jobs()))

        loop = asyncio.get_event_loop()
        while True:
            raw = await loop.run_in_executor(None, raw_queue.get)
            update = Update.de_json(raw, application.bot)
            await application.update_queue.put(update)


if __name__ == "__main__":
    asyncio.run(main())
