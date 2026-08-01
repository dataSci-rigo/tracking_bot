#!/usr/bin/env python3
"""
Single entry point for pinger.py + accountability_bot.py + franklin/.

All three now share one Telegram bot token (PING_BOT_ID, "pinger_bot").
Telegram only allows one active getUpdates long-poller per bot token —
running each bot as its own process/service caused a 409 conflict where the
losing poller silently saw zero updates forever (the original
"accountability bot ignores all replies" bug). This script owns the single
getUpdates loop and fans each update out by Telegram topic
(message_thread_id) to whichever bot it belongs to:

  4  (PINGER_THREAD_ID)         -> pinger.py
  73 (ACCOUNTABILITY_THREAD_ID) -> accountability_bot.py
  47 (FRANKLIN_THREAD_ID)       -> franklin/

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

TOKEN    = os.getenv("PING_BOT_ID", "")
BASE_URL = f"https://api.telegram.org/bot{TOKEN}"

PINGER_THREAD_ID         = int(os.getenv("PINGER_THREAD_ID", "0") or "0")
ACCOUNTABILITY_THREAD_ID = int(os.getenv("ACCOUNTABILITY_THREAD_ID", "0") or "0")
FRANKLIN_THREAD_ID       = int(os.getenv("FRANKLIN_THREAD_ID", "0") or "0")


def _thread_id_of(update: dict) -> "int | None":
    msg = update.get("message") or update.get("callback_query", {}).get("message", {})
    return msg.get("message_thread_id") if msg else None


_ALLOWED_UPDATES = [
    "message", "edited_message", "callback_query", "my_chat_member",
    "message_reaction",  # needed for Franklin's advice 👍/👎 tracking; excluded by default
]


def _poller(queues: dict, keyed_routes: dict) -> None:
    """queues: thread_id -> Queue, for normal message/callback_query updates
    (callback_query carries the full original message, including
    message_thread_id, so button taps — pinger's rating buttons,
    accountability_bot's Q3/Q3b scale buttons — already route correctly by
    topic here; no special-casing needed).
    keyed_routes: raw update key ("message_reaction") -> Queue, for update
    types that carry no message_thread_id at all and so can't be routed by
    topic — routed straight to whichever single bot currently needs them."""
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
            for key, target_q in keyed_routes.items():
                if key in upd:
                    target_q.put(upd)
                    routed = True
                    break
            if routed:
                continue

            thread_id = _thread_id_of(upd)
            target = queues.get(thread_id)
            if target is not None:
                target.put(upd)
            else:
                print(f"[poller] unrouted update (thread_id={thread_id}, update_id={upd['update_id']})")


def _run_franklin(raw_queue: "queue.Queue") -> None:
    sys.path.insert(0, str(HERE / "franklin"))
    import control as franklin_control  # todo_list/franklin/control.py
    threading.Thread(target=franklin_control.run, daemon=True, name="franklin-control").start()
    import main as franklin_main  # todo_list/franklin/main.py
    asyncio.run(franklin_main.run_fed(raw_queue))


def main() -> None:
    if not TOKEN:
        raise ValueError("PING_BOT_ID not set in .env")

    pinger_q   = queue.Queue()
    acct_q     = queue.Queue()
    franklin_q = queue.Queue()

    assignments = [
        ("PINGER_THREAD_ID", PINGER_THREAD_ID, pinger_q),
        ("ACCOUNTABILITY_THREAD_ID", ACCOUNTABILITY_THREAD_ID, acct_q),
        ("FRANKLIN_THREAD_ID", FRANKLIN_THREAD_ID, franklin_q),
    ]
    queues: dict = {}
    owners: dict = {}  # thread_id -> env var name that claimed it, to detect collisions
    for env_name, thread_id, q in assignments:
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

    print(f"Routing topics: pinger={PINGER_THREAD_ID}  accountability={ACCOUNTABILITY_THREAD_ID}  franklin={FRANKLIN_THREAD_ID}")

    keyed_routes = {"message_reaction": franklin_q}
    threading.Thread(target=_poller, args=(queues, keyed_routes), daemon=True, name="poller").start()

    import pinger
    import accountability_bot

    workers = [
        threading.Thread(target=pinger.run, args=(pinger_q,), name="pinger"),
        threading.Thread(target=accountability_bot.run, args=(acct_q,), name="accountability"),
        threading.Thread(target=_run_franklin, args=(franklin_q,), name="franklin"),
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
