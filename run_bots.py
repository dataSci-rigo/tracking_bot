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


def _poller(queues: dict) -> None:
    offset = 0
    while True:
        try:
            resp = requests.get(
                f"{BASE_URL}/getUpdates",
                params={"timeout": 30, "offset": offset, "limit": 100},
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
            thread_id = _thread_id_of(upd)
            target = queues.get(thread_id)
            if target is not None:
                target.put(upd)
            else:
                print(f"[poller] unrouted update (thread_id={thread_id}, update_id={upd['update_id']})")


def _run_franklin(raw_queue: "queue.Queue") -> None:
    sys.path.insert(0, str(HERE / "franklin"))
    import main as franklin_main  # todo_list/franklin/main.py
    asyncio.run(franklin_main.run_fed(raw_queue))


def main() -> None:
    if not TOKEN:
        raise ValueError("PING_BOT_ID not set in .env")

    pinger_q   = queue.Queue()
    acct_q     = queue.Queue()
    franklin_q = queue.Queue()

    queues = {}
    if PINGER_THREAD_ID:
        queues[PINGER_THREAD_ID] = pinger_q
    if ACCOUNTABILITY_THREAD_ID:
        queues[ACCOUNTABILITY_THREAD_ID] = acct_q
    if FRANKLIN_THREAD_ID:
        queues[FRANKLIN_THREAD_ID] = franklin_q

    print(f"Routing topics: pinger={PINGER_THREAD_ID}  accountability={ACCOUNTABILITY_THREAD_ID}  franklin={FRANKLIN_THREAD_ID}")

    threading.Thread(target=_poller, args=(queues,), daemon=True, name="poller").start()

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
