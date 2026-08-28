#!/usr/bin/env python3
"""
One-off: create the "Willpower" and "ADHD Tasks" forum topics in the
tracking group (PINGER_CHANNEL_ID) and print their thread IDs, for
WPI_THREAD_ID / ADHD_THREAD_ID in .env. Requires the bot to have
can_manage_topics (verified true via test_telegram_topics.py).

Run:  python create_topics.py
"""
import os

import requests
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("PING_BOT_ID", "")
CHAT_ID = int(os.getenv("PINGER_CHANNEL_ID", "0") or "0")
BASE_URL = f"https://api.telegram.org/bot{TOKEN}"

TOPICS = [
    ("Willpower", "WPI_THREAD_ID"),
    ("ADHD Tasks", "ADHD_THREAD_ID"),
]


def main() -> None:
    if not TOKEN or not CHAT_ID:
        raise ValueError("PING_BOT_ID / PINGER_CHANNEL_ID not set in .env")
    for name, env_key in TOPICS:
        resp = requests.post(
            f"{BASE_URL}/createForumTopic",
            json={"chat_id": CHAT_ID, "name": name},
            timeout=10,
        ).json()
        if resp.get("ok"):
            thread_id = resp["result"]["message_thread_id"]
            print(f"{env_key}={thread_id}   # topic '{name}'")
        else:
            print(f"FAILED to create '{name}': {resp}")


if __name__ == "__main__":
    main()
