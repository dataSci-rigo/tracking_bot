from __future__ import annotations

import os
import re


def estimate_task_minutes(description: str) -> int | None:
    """Call Claude to estimate how long a task will take. Returns minutes or None on failure."""
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=32,
            messages=[{
                "role": "user",
                "content": (
                    f'How many minutes would a typical person realistically need to complete this task: "{description}"? '
                    "Reply with ONLY a single integer. No explanation, no range, just the number."
                ),
            }],
        )
        text = msg.content[0].text.strip()
        m = re.search(r"\d+", text)
        return int(m.group()) if m else None
    except Exception:
        return None
