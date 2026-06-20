import anthropic
import store

_client = anthropic.Anthropic()


class CoachError(Exception):
    pass


def reflect_on_virtue(virtue_id: str, weekly_recap: bool = False) -> str:
    config = store.load_config()
    virtue = next((v for v in config["virtues"] if v["id"] == virtue_id), None)
    if virtue is None:
        raise CoachError(f"Unknown virtue: {virtue_id}")

    from datetime import date, timedelta
    window = config.get("coach_window_days", 90)
    end = date.today()
    start = end - timedelta(days=window)

    marks = store.get_marks(start.isoformat(), end.isoformat())
    all_todos = store.load_data().get("todos", [])
    relevant_todos = [t for t in all_todos if t.get("virtue") == virtue_id and t["created"][:10] >= start.isoformat()]
    notes = store.get_notes(virtue=virtue_id, since=start.isoformat())

    lines = [
        f"Focus virtue: {virtue['name']}",
        f"Precept: {virtue['precept']}",
        "",
    ]

    fault_lines = [(d, v.get(virtue_id, 0)) for d, v in sorted(marks.items()) if virtue_id in v]
    if fault_lines:
        lines.append(f"Last {window} days of faults (count per day):")
        for d, count in fault_lines:
            lines.append(f"  {d}: {count}")
    else:
        lines.append(f"No fault marks recorded in the last {window} days.")
    lines.append("")

    if relevant_todos:
        lines.append("Todos opened in this period:")
        for t in relevant_todos:
            status_note = f"({t['status']} {t.get('updated', t['created'])[:10]})" if t["status"] != "open" else "(open)"
            lines.append(f"  - {t['id']} {status_note}: {t['text']}")
    else:
        lines.append("No todos in this period.")
    lines.append("")

    if notes:
        lines.append("Notes:")
        for n in notes:
            lines.append(f"  - {n['ts'][:10]}: {n['text']}")
    else:
        lines.append("No notes in this period.")

    user_message = "\n".join(lines)

    if weekly_recap:
        system = (
            "You are a thoughtful reflection partner helping the user practice Benjamin Franklin's "
            "13-virtue method. Look at this week's data for a single virtue and give a concise "
            "weekly recap: what went well, what didn't, and one specific suggestion for next week. "
            "Be direct and encouraging. 2-3 short paragraphs. No bullet lists, no headers."
        )
        max_tokens = 512
    else:
        system = (
            "You are a thoughtful reflection partner helping the user practice Benjamin Franklin's "
            "13-virtue method. Look at the data for a single virtue and identify patterns, point out "
            "one or two things that seem to be working, and suggest one concrete experiment for the "
            "coming week. Be concise — 4-6 short paragraphs. No bullet lists, no headers."
        )
        max_tokens = 1024

    try:
        response = _client.messages.create(
            model=config.get("coach_model", "claude-sonnet-4-6"),
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user_message}],
        )
        return response.content[0].text
    except Exception as e:
        raise CoachError(str(e)) from e


def refresh_inspiration(virtue_id: str) -> str | None:
    config = store.load_config()
    virtue = next((v for v in config["virtues"] if v["id"] == virtue_id), None)
    if virtue is None:
        return None

    system = (
        "You are writing a brief daily-practice guide for one of Benjamin Franklin's 13 virtues. "
        "Write exactly three sections with these bold headers: "
        "**Why this matters**, **What to do**, **Hangups**. "
        "Each section is 2-4 sentences. Be concrete and practical. Total length ~150 words."
    )
    user_message = (
        f"Virtue: {virtue['name']}\nPrecept: {virtue['precept']}\n\n"
        "Write a fresh framing for practicing this virtue this week."
    )

    try:
        response = _client.messages.create(
            model=config.get("coach_model", "claude-sonnet-4-6"),
            max_tokens=512,
            system=system,
            messages=[{"role": "user", "content": user_message}],
        )
        return response.content[0].text
    except Exception:
        return None
