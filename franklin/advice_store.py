"""
Storage for the daily virtue affirmations/advice feature.

- virtue_advice_examples.md — human-readable, accumulates 👍/👎-reacted
  examples and reply notes. Read back in whole as few-shot context when
  coach.generate_daily_advice() asks Claude for the next batch.
- advice_data.json — machine-readable, tracks which items were generated
  for which day, whether/where they were sent, so incoming reactions and
  replies can be matched back to the specific advice text.
"""
import json
from pathlib import Path

_DIR = Path(__file__).parent
EXAMPLES_MD_PATH = _DIR / "virtue_advice_examples.md"
DATA_PATH        = _DIR / "advice_data.json"

_SECTIONS = ["Good examples (👍)", "Bad examples (👎)", "Notes"]
_MD_TITLE = (
    "# Virtue Advice — Examples\n\n"
    "Accumulated feedback used as few-shot context when generating daily "
    "affirmations/advice. Reacting 👍/👎 to an advice message files it below; "
    "replying to one with a note appends that note.\n"
)


# ── examples.md ────────────────────────────────────────────────────────────

def _parse_sections(text: str) -> dict:
    sections = {name: [] for name in _SECTIONS}
    current = None
    for line in text.splitlines():
        if line.startswith("## "):
            name = line[3:].strip()
            current = name if name in sections else None
            continue
        if current and line.strip():
            sections[current].append(line.rstrip())
    return sections


def _render(sections: dict) -> str:
    lines = [_MD_TITLE.rstrip("\n"), ""]
    for name in _SECTIONS:
        lines.append(f"## {name}")
        lines.append("")
        lines.extend(sections[name])
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _ensure_md() -> None:
    if not EXAMPLES_MD_PATH.exists():
        EXAMPLES_MD_PATH.write_text(_render({name: [] for name in _SECTIONS}))


def read_examples_md() -> str:
    _ensure_md()
    return EXAMPLES_MD_PATH.read_text()


def _append_line(header: str, line: str) -> None:
    _ensure_md()
    sections = _parse_sections(EXAMPLES_MD_PATH.read_text())
    sections[header].append(line)
    EXAMPLES_MD_PATH.write_text(_render(sections))


def append_good_example(virtue_name: str, advice_text: str) -> None:
    _append_line("Good examples (👍)", f"- [{virtue_name}] {advice_text}")


def append_bad_example(virtue_name: str, advice_text: str) -> None:
    _append_line("Bad examples (👎)", f"- [{virtue_name}] {advice_text}")


def append_note(note_text: str, advice_text: str) -> None:
    line = f'The user said """{note_text}""" about """{advice_text}""".'
    _append_line("Notes", line)


# ── advice_data.json ─────────────────────────────────────────────────────

def _load() -> dict:
    if not DATA_PATH.exists():
        return {"days": {}}
    return json.loads(DATA_PATH.read_text())


def _save(data: dict) -> None:
    DATA_PATH.write_text(json.dumps(data, indent=2))


def save_daily_advice(day: str, virtue_id: str, virtue_name: str, texts: list) -> None:
    data = _load()
    data.setdefault("days", {})[day] = {
        "virtue_id": virtue_id,
        "virtue_name": virtue_name,
        "items": [
            {"idx": i, "text": t, "message_id": None, "sent": False}
            for i, t in enumerate(texts)
        ],
    }
    _save(data)


def take_next_unsent(day: str) -> "dict | None":
    day_entry = _load().get("days", {}).get(day)
    if not day_entry:
        return None
    for item in day_entry["items"]:
        if not item["sent"]:
            return item
    return None


def mark_sent(day: str, idx: int, message_id: int) -> None:
    data = _load()
    day_entry = data.get("days", {}).get(day)
    if not day_entry:
        return
    for item in day_entry["items"]:
        if item["idx"] == idx:
            item["sent"] = True
            item["message_id"] = message_id
            break
    _save(data)


def find_by_message_id(message_id: int) -> "tuple[str, dict] | None":
    for day, entry in _load().get("days", {}).items():
        for item in entry["items"]:
            if item.get("message_id") == message_id:
                return day, {**item, "virtue_name": entry["virtue_name"]}
    return None
