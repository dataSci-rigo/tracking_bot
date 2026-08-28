#!/usr/bin/env python3
"""
Single entry point for the five todo_list bots:
pinger.py + accountability_bot.py + franklin/ + wpi/ + adhd/.

All five share one Telegram bot token (PING_BOT_ID, "pinger_bot").
Telegram only allows one active getUpdates long-poller per bot token —
running bots as separate processes/services caused a 409 conflict where the
losing poller silently saw zero updates forever (the original
"accountability bot ignores all replies" bug). This script owns the single
getUpdates loop and fans each update out by Telegram topic
(message_thread_id) to whichever bot it belongs to:

  4    (PINGER_THREAD_ID)         -> pinger.py
  73   (ACCOUNTABILITY_THREAD_ID) -> accountability_bot.py
  47   (FRANKLIN_THREAD_ID)       -> franklin/
  2998 (WPI_THREAD_ID)            -> wpi/   (Willpower Instinct)
  2999 (ADHD_THREAD_ID)           -> adhd/  (ADHD task bot)

Each bot can be individually toggled on/off (bot_state.py) via the control
API (control_server.py, port RUNBOTS_CONTROL_PORT) or the a-bot panel.
A disabled bot's incoming updates are dropped with a throttled one-line
auto-reply in its topic, and its scheduled sends skip.

Run:   python run_bots.py
Stop:  Ctrl-C
"""
import asyncio
import json
import os
import queue
import sys
import threading
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

import bot_state

TOKEN    = os.getenv("PING_BOT_ID", "")
BASE_URL = f"https://api.telegram.org/bot{TOKEN}"
CHANNEL_ID = int(os.getenv("PINGER_CHANNEL_ID", "0") or "0")

PINGER_THREAD_ID         = int(os.getenv("PINGER_THREAD_ID", "0") or "0")
ACCOUNTABILITY_THREAD_ID = int(os.getenv("ACCOUNTABILITY_THREAD_ID", "0") or "0")
FRANKLIN_THREAD_ID       = int(os.getenv("FRANKLIN_THREAD_ID", "0") or "0")
WPI_THREAD_ID            = int(os.getenv("WPI_THREAD_ID", "0") or "0")
ADHD_THREAD_ID           = int(os.getenv("ADHD_THREAD_ID", "0") or "0")

OFF_REPLY_THROTTLE_SEC = 600  # at most one "bot is off" notice per bot per 10 min
_off_reply_last: dict = {}

_BOT_LABELS = {
    "pinger": "Pinger",
    "accountability": "Accountability Bot",
    "franklin": "Franklin",
    "wpi": "Willpower Instinct",
    "adhd": "ADHD Bot",
}


def _thread_id_of(update: dict) -> "int | None":
    msg = update.get("message") or update.get("callback_query", {}).get("message", {})
    return msg.get("message_thread_id") if msg else None


def _send_off_notice(bot_name: str, thread_id: int) -> None:
    now = time.time()
    if now - _off_reply_last.get(bot_name, 0) < OFF_REPLY_THROTTLE_SEC:
        return
    _off_reply_last[bot_name] = now
    try:
        requests.post(
            f"{BASE_URL}/sendMessage",
            json={
                "chat_id": CHANNEL_ID,
                "message_thread_id": thread_id,
                "text": f"🔴 {_BOT_LABELS.get(bot_name, bot_name)} is off — turn it on from the control panel.",
            },
            timeout=10,
        )
    except Exception as e:
        print(f"[poller] off-notice error: {e}")


_ALLOWED_UPDATES = [
    "message", "edited_message", "callback_query", "my_chat_member",
    "message_reaction",  # needed for Franklin's advice 👍/👎 tracking; excluded by default
]


def _poller(queues: dict, names: dict, keyed_routes: dict) -> None:
    """queues: thread_id -> Queue, for normal message/callback_query updates
    (callback_query carries the full original message, including
    message_thread_id, so button taps route correctly by topic here).
    names: thread_id -> bot name, for the per-bot on/off gate.
    keyed_routes: raw update key ("message_reaction") -> (bot_name, Queue),
    for update types that carry no message_thread_id at all and so can't be
    routed by topic — routed straight to the single bot that needs them."""
    offset = 0
    while True:
        try:
            resp = requests.get(
                f"{BASE_URL}/getUpdates",
                params={
                    "timeout": 30, "offset": offset, "limit": 100,
                    "allowed_updates": json.dumps(_ALLOWED_UPDATES),
                },
                timeout=40,
            )
            data = resp.json()
        except Exception as e:
            print(f"[poller] error: {e}")
            time.sleep(3)
            continue

        if not data.get("ok"):
            print(f"[poller] getUpdates not ok: {data}")
            time.sleep(3)
            continue

        for upd in data.get("result", []):
            offset = upd["update_id"] + 1

            routed = False
            for key, (bot_name, target_q) in keyed_routes.items():
                if key in upd:
                    if bot_state.enabled(bot_name):
                        target_q.put(upd)
                    routed = True
                    break
            if routed:
                continue

            thread_id = _thread_id_of(upd)
            target = queues.get(thread_id)
            if target is None:
                print(f"[poller] unrouted update (thread_id={thread_id}, update_id={upd['update_id']})")
                continue

            bot_name = names.get(thread_id, "")
            if bot_name and not bot_state.enabled(bot_name):
                _send_off_notice(bot_name, thread_id)
                continue

            target.put(upd)


def _retrying(name: str, coro_factory) -> None:
    """Run an async bot forever, restarting it after transient failures.
    Five PTB bots calling getMe at once on startup means an occasional
    httpx ConnectError — without this, one blip permanently kills that
    bot's thread while the other four keep running."""
    while True:
        try:
            asyncio.run(coro_factory())
            return  # clean exit
        except Exception as e:
            print(f"[{name}] crashed: {e!r} — restarting in 10s")
            time.sleep(10)


def _run_franklin(raw_queue: "queue.Queue") -> None:
    sys.path.insert(0, str(HERE / "franklin"))
    import control as franklin_control  # todo_list/franklin/control.py
    threading.Thread(target=franklin_control.run, daemon=True, name="franklin-control").start()
    import main as franklin_main  # todo_list/franklin/main.py
    _retrying("franklin", lambda: franklin_main.run_fed(raw_queue))


def _run_wpi(raw_queue: "queue.Queue") -> None:
    from wpi import main as wpi_main
    _retrying("wpi", lambda: wpi_main.run_fed(raw_queue))


def _run_adhd(raw_queue: "queue.Queue") -> None:
    from adhd import bot as adhd_bot
    _retrying("adhd", lambda: adhd_bot.run_fed(raw_queue))


def main() -> None:
    if not TOKEN:
        raise ValueError("PING_BOT_ID not set in .env")

    pinger_q   = queue.Queue()
    acct_q     = queue.Queue()
    franklin_q = queue.Queue()
    wpi_q      = queue.Queue()
    adhd_q     = queue.Queue()

    assignments = [
        ("pinger",         "PINGER_THREAD_ID", PINGER_THREAD_ID, pinger_q),
        ("accountability", "ACCOUNTABILITY_THREAD_ID", ACCOUNTABILITY_THREAD_ID, acct_q),
        ("franklin",       "FRANKLIN_THREAD_ID", FRANKLIN_THREAD_ID, franklin_q),
        ("wpi",            "WPI_THREAD_ID", WPI_THREAD_ID, wpi_q),
        ("adhd",           "ADHD_THREAD_ID", ADHD_THREAD_ID, adhd_q),
    ]
    queues: dict = {}
    names: dict = {}
    owners: dict = {}  # thread_id -> env var name that claimed it, to detect collisions
    for bot_name, env_name, thread_id, q in assignments:
        if not thread_id:
            continue
        if thread_id in owners:
            raise ValueError(
                f"Thread ID collision: {env_name}={thread_id} is already claimed by "
                f"{owners[thread_id]}. Two bots can't share a topic — fix the .env "
                f"(check for a stale/copy-pasted value)."
            )
        owners[thread_id] = env_name
        queues[thread_id] = q
        names[thread_id] = bot_name

    print(
        f"Routing topics: pinger={PINGER_THREAD_ID}  accountability={ACCOUNTABILITY_THREAD_ID}  "
        f"franklin={FRANKLIN_THREAD_ID}  wpi={WPI_THREAD_ID}  adhd={ADHD_THREAD_ID}"
    )
    print(f"Bot states: {bot_state.all_states()}")

    keyed_routes = {"message_reaction": ("franklin", franklin_q)}
    threading.Thread(target=_poller, args=(queues, names, keyed_routes), daemon=True, name="poller").start()

    import control_server
    threading.Thread(target=control_server.run, daemon=True, name="control-server").start()

    import pinger
    import accountability_bot

    workers = [
        threading.Thread(target=pinger.run, args=(pinger_q,), name="pinger"),
        threading.Thread(target=accountability_bot.run, args=(acct_q,), name="accountability"),
        threading.Thread(target=_run_franklin, args=(franklin_q,), name="franklin"),
        threading.Thread(target=_run_wpi, args=(wpi_q,), name="wpi"),
        threading.Thread(target=_run_adhd, args=(adhd_q,), name="adhd"),
    ]
    for t in workers:
        t.start()
    for t in workers:
        t.join()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
