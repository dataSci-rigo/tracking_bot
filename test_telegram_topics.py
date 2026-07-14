#!/usr/bin/env python3
"""
One-off diagnostic: confirm whether PINGER_CHANNEL_ID is a real Telegram
"Topics" (forum) group, and whether message_thread_id is reliably reported
on incoming replies for topics 4 (pinger), 47 (franklin), 73 (accountability).

Sends one labeled message into each topic, then long-polls and prints
chat_id / message_thread_id / message_id / reply_to_message.message_id / text
for every incoming update so you can reply in each topic and watch what
comes back.

Run:  python test_telegram_topics.py
Stop: Ctrl-C
"""
import json
import os
import time

import requests
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("PING_BOT_ID", "")
CHAT_ID = int(os.getenv("PINGER_CHANNEL_ID", "0") or "0")
BASE_URL = f"https://api.telegram.org/bot{TOKEN}"

TOPICS = {
    4: "pinger",
    47: "franklin",
    73: "accountability",
}


def get_chat_info() -> None:
    resp = requests.get(f"{BASE_URL}/getChat", params={"chat_id": CHAT_ID}, timeout=10)
    data = resp.json()
    print("getChat response:")
    print(json.dumps(data, indent=2))
    result = data.get("result", {})
    print(f"\n  is_forum = {result.get('is_forum', '(field absent -> false)')}")


def send_test_messages() -> None:
    print("\nSending one test message into each topic...")
    for thread_id, label in TOPICS.items():
        payload = {
            "chat_id": CHAT_ID,
            "text": f"Test ({label}, topic {thread_id}) — please reply to this message.",
            "message_thread_id": thread_id,
        }
        resp = requests.post(f"{BASE_URL}/sendMessage", json=payload, timeout=10)
        data = resp.json()
        if data.get("ok"):
            msg = data["result"]
            print(f"  sent to topic {thread_id} ({label}): message_id={msg['message_id']}")
        else:
            print(f"  FAILED to send to topic {thread_id} ({label}): {data}")


def listen_for_replies(duration_sec: int = 120) -> None:
    print(f"\nListening for replies for up to {duration_sec}s. Reply in each topic now...")
    offset = 0
    deadline = time.time() + duration_sec
    while time.time() < deadline:
        remaining = max(1, int(deadline - time.time()))
        resp = requests.get(
            f"{BASE_URL}/getUpdates",
            params={"timeout": min(30, remaining), "offset": offset, "limit": 100},
            timeout=min(30, remaining) + 10,
        )
        data = resp.json()
        if not data.get("ok"):
            print(f"  poll error: {data}")
            time.sleep(2)
            continue
        updates = data.get("result", [])
        for upd in updates:
            offset = upd["update_id"] + 1
            msg = upd.get("message")
            if not msg:
                continue
            thread_id = msg.get("message_thread_id")
            reply_to = msg.get("reply_to_message", {}).get("message_id")
            print(
                f"  chat_id={msg.get('chat', {}).get('id')}  "
                f"message_thread_id={thread_id}  "
                f"message_id={msg.get('message_id')}  "
                f"reply_to={reply_to}  "
                f"text={msg.get('text')!r}"
            )
    print("\nDone listening.")


def main() -> None:
    if not TOKEN:
        raise ValueError("PING_BOT_ID not set in .env")
    if not CHAT_ID:
        raise ValueError("PINGER_CHANNEL_ID not set in .env")

    get_chat_info()
    send_test_messages()
    listen_for_replies(duration_sec=120)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
