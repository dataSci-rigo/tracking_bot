#!/usr/bin/env python3
"""
Accountability Bot — random check-ins via Telegram channel thread.

Schedule (all times America/Los_Angeles):
  07:00        "What's your plan for today?" (waits up to 30 min)
  07:00–23:00  random 3-question check-ins every 1–2.5 hr
  23:00        "How much did you get done today?" (waits up to 30 min)
  23:00–07:00  silent

Run:   python accountability_bot.py
Test:  python accountability_bot.py --test   (one check-in immediately)

.env keys:
  PING_BOT_ID            — Telegram bot token
  PINGER_CHANNEL_ID      — e.g. -1003955681692
  ACCOUNTABILITY_THREAD_ID — e.g. 73
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

logging.basicConfig(level=logging.WARNING)

TOKEN      = os.getenv("PING_BOT_ID", "")
CHANNEL_ID = int(os.getenv("PINGER_CHANNEL_ID", "0") or "0")
THREAD_ID  = int(os.getenv("ACCOUNTABILITY_THREAD_ID", "0") or "0")

DATA_FILE  = Path(__file__).parent / "accountability_data.json"
PAUSE_FILE = Path(__file__).parent / "pause_state.json"

PT = ZoneInfo("America/Los_Angeles")

WINDOW_START  = 7    # 7 AM PT
WINDOW_END    = 23   # 11 PM PT
MIN_GAP_MIN   = 60
MAX_GAP_MIN   = 150
REPLY_TIMEOUT = 600  # 10 min per question
MAX_RETRIES   = 2

QUESTIONS = [
    "What are you doing?",
    "What should you be doing?",
    "Are you working hard?",
]

# asyncio queue for incoming messages during a check-in session
_inbox: asyncio.Queue[str] = asyncio.Queue()


# ── Time helpers ──────────────────────────────────────────────────────────────

def now_pt() -> datetime:
    return datetime.now(tz=PT)

def now_str() -> str:
    return now_pt().strftime("%Y-%m-%d %H:%M:%S")

def today_str() -> str:
    return now_pt().strftime("%Y-%m-%d")

def seconds_until_hour_pt(hour: int) -> float:
    now    = now_pt()
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    return (target - now).total_seconds()


# ── Data helpers ──────────────────────────────────────────────────────────────

def load_data() -> dict:
    if not DATA_FILE.exists():
        return {"sessions": [], "days": {}}
    raw = json.loads(DATA_FILE.read_text())
    if isinstance(raw, list):
        return {"sessions": raw, "days": {}}
    raw.setdefault("days", {})
    return raw

def save_data(data: dict) -> None:
    DATA_FILE.write_text(json.dumps(data, indent=2))


# ── Pause helpers ─────────────────────────────────────────────────────────────

def is_paused() -> bool:
    if not PAUSE_FILE.exists():
        return False
    state = json.loads(PAUSE_FILE.read_text())
    resume_at = state.get("resume_at")
    if resume_at and datetime.fromisoformat(resume_at) <= now_pt():
        PAUSE_FILE.unlink(missing_ok=True)
        return False
    return True

def _write_pause(resume_at: datetime) -> None:
    PAUSE_FILE.write_text(json.dumps({
        "paused_at": now_pt().isoformat(),
        "resume_at": resume_at.isoformat(),
    }, indent=2))


# ── Telegram send helpers ─────────────────────────────────────────────────────

async def send(bot: Bot, text: str) -> None:
    await bot.send_message(
        chat_id=CHANNEL_ID,
        message_thread_id=THREAD_ID,
        text=text,
    )


# ── Wait for reply ────────────────────────────────────────────────────────────

async def wait_for_reply(timeout: float) -> str | None:
    # Drain stale messages first
    while not _inbox.empty():
        try:
            _inbox.get_nowait()
        except asyncio.QueueEmpty:
            break
    try:
        return await asyncio.wait_for(_inbox.get(), timeout=timeout)
    except asyncio.TimeoutError:
        return None


# ── Sessions ──────────────────────────────────────────────────────────────────

async def morning_session(bot: Bot) -> None:
    print(f"[{now_str()}] Morning session")
    await send(bot, "Good morning! What's your plan for today?")
    plan = await wait_for_reply(timeout=30 * 60)
    data = load_data()
    data["days"].setdefault(today_str(), {})["plan"] = plan or "(no response)"
    save_data(data)
    if plan:
        await send(bot, "Got it! Good luck today.")
    else:
        print("  No plan response — moving on")


async def evening_session(bot: Bot) -> None:
    print(f"[{now_str()}] Evening session")
    await send(bot, "How much did you get done today?")
    review = await wait_for_reply(timeout=30 * 60)
    data = load_data()
    data["days"].setdefault(today_str(), {})["evening_review"] = review or "(no response)"
    save_data(data)
    if review:
        await send(bot, "Nice work today. Rest up!")
    else:
        print("  No review response — moving on")


async def run_checkin(bot: Bot) -> None:
    print(f"[{now_str()}] Starting check-in")
    await send(bot, "Hey! Quick accountability check-in — 3 questions.")
    await asyncio.sleep(0.8)

    collected: list[tuple[str, str]] = []

    for idx, question in enumerate(QUESTIONS, 1):
        answer = None
        for attempt in range(MAX_RETRIES + 1):
            if attempt == 0:
                await send(bot, f"{idx}. {question}")
            else:
                await send(bot, f"Still waiting on Q{idx}: {question}")

            answer = await wait_for_reply(timeout=REPLY_TIMEOUT)
            if answer:
                break

        if answer is None:
            await send(bot, "No response after 2 attempts. Dropping this check-in — I'll try again later.")
            data = load_data()
            data["sessions"].append({
                "timestamp":           now_pt().isoformat(),
                "status":              "dropped",
                "dropped_on_question": idx,
                "questions": [{"question": q, "answer": None} for q in QUESTIONS],
            })
            save_data(data)
            return

        collected.append((question, answer))
        if idx < len(QUESTIONS):
            await asyncio.sleep(0.8)

    await send(bot, "Thanks for checking in! Stay focused.")
    print(f"[{now_str()}] Check-in complete")
    data = load_data()
    data["sessions"].append({
        "timestamp": now_pt().isoformat(),
        "status":    "completed",
        "questions": [{"question": q, "answer": a} for q, a in collected],
    })
    save_data(data)


# ── Main loop (runs as a background task) ────────────────────────────────────

async def main_loop(bot: Bot) -> None:
    while True:
        now   = now_pt()
        today = today_str()
        data  = load_data()

        if is_paused():
            if now.hour >= WINDOW_END and "evening_review" not in data["days"].get(today, {}):
                await evening_session(bot)
            else:
                await asyncio.sleep(60)
            continue

        if now.hour < WINDOW_START:
            wait = seconds_until_hour_pt(WINDOW_START)
            print(f"[{now_str()}] Before window. Sleeping {wait/3600:.1f}h until {WINDOW_START}:00 PT")
            await asyncio.sleep(wait)
            continue

        if now.hour >= WINDOW_END:
            if "evening_review" not in data["days"].get(today, {}):
                await evening_session(bot)
            wait = seconds_until_hour_pt(WINDOW_START)
            print(f"[{now_str()}] Evening done. Sleeping {wait/3600:.1f}h until {WINDOW_START}:00 PT")
            await asyncio.sleep(wait)
            continue

        if "plan" not in data["days"].get(today, {}):
            await morning_session(bot)
            continue

        gap_sec      = random.randint(MIN_GAP_MIN * 60, MAX_GAP_MIN * 60)
        window_close = now.replace(hour=WINDOW_END, minute=0, second=0, microsecond=0)
        closes_in    = (window_close - now).total_seconds()

        if gap_sec > closes_in:
            print(f"[{now_str()}] Gap exceeds window. Waiting until {WINDOW_END}:00 PT")
            await asyncio.sleep(closes_in)
            continue

        wake_at = now + timedelta(seconds=gap_sec)
        print(f"[{now_str()}] Next check-in at {wake_at.strftime('%H:%M')} PT ({gap_sec // 60}m)")
        await asyncio.sleep(gap_sec)

        if WINDOW_START <= now_pt().hour < WINDOW_END and not is_paused():
            try:
                await run_checkin(bot)
            except Exception as e:
                print(f"[{now_str()}] ERROR in check-in: {e}")


# ── Telegram update handlers ──────────────────────────────────────────────────

def _in_thread(update: Update) -> bool:
    msg = update.effective_message
    if msg is None:
        return False
    return msg.chat_id == CHANNEL_ID and msg.message_thread_id == THREAD_ID


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _in_thread(update):
        return
    text = (update.effective_message.text or "").strip()
    if not text or text.startswith("/"):
        return
    await _inbox.put(text)


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _in_thread(update):
        return
    bot = context.bot
    args_text = " ".join(context.args or [])
    try:
        hours = float(args_text) if args_text else None
    except ValueError:
        await send(bot, "Usage: /pause [hours] — e.g. /pause 2 or /pause for rest of day")
        return

    now = now_pt()
    if hours is not None:
        resume_at = now + timedelta(hours=hours)
        msg = f"Paused for {hours:.4g}h (until {resume_at.strftime('%H:%M')} PT). /resume to end early."
    else:
        resume_at = now.replace(hour=WINDOW_END, minute=0, second=0, microsecond=0)
        if now >= resume_at:
            resume_at += timedelta(days=1)
        msg = f"Paused for the rest of the day (until {WINDOW_END}:00 PT). Evening review will still run."
    _write_pause(resume_at)
    await send(bot, msg)


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _in_thread(update):
        return
    if is_paused():
        PAUSE_FILE.unlink(missing_ok=True)
        await send(context.bot, "Resumed! Check-ins will continue.")
    else:
        await send(context.bot, "Bot is not paused.")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _in_thread(update):
        return
    if is_paused():
        state = json.loads(PAUSE_FILE.read_text())
        await send(context.bot, f"Paused since {state['paused_at'][:16]}\nResumes at: {state.get('resume_at','?')[:16]}")
    else:
        await send(context.bot, "Running normally.")


async def cmd_sum(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _in_thread(update):
        return
    summary = " ".join(context.args or [])
    if not summary:
        await send(context.bot, "Usage: /sum <your summary>")
        return
    data = load_data()
    data["days"].setdefault(today_str(), {})["summary"] = summary
    save_data(data)
    await send(context.bot, f"Summary saved: {summary}")


async def cmd_goals(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _in_thread(update):
        return
    goals = " ".join(context.args or [])
    if not goals:
        await send(context.bot, "Usage: /goals <your goals>")
        return
    data = load_data()
    data["days"].setdefault(today_str(), {})["goals"] = goals
    save_data(data)
    await send(context.bot, f"Goals saved: {goals}")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _in_thread(update):
        return
    await send(context.bot, (
        "Accountability Bot\n\n"
        "/pause [hours] — pause check-ins\n"
        "/resume — end a pause early\n"
        "/status — show pause state\n"
        "/sum <text> — save a daily summary\n"
        "/goals <text> — save today's goals\n"
        "/help — show this message\n\n"
        f"Schedule (PT): {WINDOW_START}:00 morning plan · check-ins every "
        f"{MIN_GAP_MIN}–{MAX_GAP_MIN} min · {WINDOW_END}:00 evening review"
    ))


# ── Entry point ───────────────────────────────────────────────────────────────

async def _post_init(application: Application) -> None:
    asyncio.create_task(main_loop(application.bot))


def build_application() -> Application:
    app = (
        Application.builder()
        .token(TOKEN)
        .post_init(_post_init)
        .build()
    )
    app.add_handler(CommandHandler("pause",  cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("sum",    cmd_sum))
    app.add_handler(CommandHandler("goals",  cmd_goals))
    app.add_handler(CommandHandler("help",   cmd_help))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    return app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true", help="Run one check-in immediately then exit")
    args = parser.parse_args()

    if not TOKEN:
        raise ValueError("PING_BOT_ID not set in .env")
    if not CHANNEL_ID:
        raise ValueError("PINGER_CHANNEL_ID not set in .env")
    if not THREAD_ID:
        raise ValueError("ACCOUNTABILITY_THREAD_ID not set in .env")

    if args.test:
        async def _test():
            async with Bot(TOKEN) as bot:
                await run_checkin(bot)
        asyncio.run(_test())
        return

    print(f"Accountability bot started. Window: {WINDOW_START}:00–{WINDOW_END}:00 PT → thread {THREAD_ID}")
    build_application().run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
