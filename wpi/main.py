import logging

from telegram import Update
from telegram.ext import Application

from . import db
from .config import TELEGRAM_TOKEN, OWNER_CHAT_ID, EVENING_HOUR, TIMEZONE
from .handlers import register_handlers, _send_energy_prompt
from .scheduler import register_jobs

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


async def post_init(application: Application) -> None:
    from datetime import date, datetime, timedelta
    from zoneinfo import ZoneInfo

    await db.init_db()
    logger.info("Database initialised")

    cycle = await db.get_active_cycle(OWNER_CHAT_ID)
    if not cycle:
        return

    now_local = datetime.now(ZoneInfo(TIMEZONE))
    today = now_local.date()

    # ── Catch-up: evening check-in if missed AND within the same evening ──────
    # Only send if it's between 9 PM and 11:59 PM — not during daytime restarts.
    if EVENING_HOUR <= now_local.hour <= 23:
        entry = await db.get_daily_entry(cycle["id"], today.isoformat())
        if not entry or not entry.get("energy_level"):
            logger.info("Catch-up: sending missed evening check-in for %s", today)

            class _FakeContext:
                def __init__(self, bot):
                    self.bot = bot
                    self.user_data = {}

            await _send_energy_prompt(_FakeContext(application.bot), OWNER_CHAT_ID)


def _build_app() -> Application:
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .build()
    )
    register_handlers(app)
    register_jobs(app)
    return app


async def run_fed(raw_queue) -> None:
    """Run fed by an external queue.Queue of raw Telegram update dicts,
    instead of polling getUpdates itself. Used by todo_list/run_bots.py —
    the shared PING_BOT_ID token allows only one poller, which routes this
    bot's topic (WPI_THREAD_ID) here. Application.start() also starts the
    PTB job_queue, so the four scheduled jobs keep working."""
    import asyncio

    app = _build_app()
    async with app:
        await app.start()
        logger.info("Willpower Instinct started (fed mode)")
        loop = asyncio.get_event_loop()
        while True:
            raw = await loop.run_in_executor(None, raw_queue.get)
            update = Update.de_json(raw, app.bot)
            await app.update_queue.put(update)


def main() -> None:
    app = _build_app()
    logger.info("Willpower Instinct bot starting (standalone polling)")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
