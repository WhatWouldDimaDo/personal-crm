"""Minimal notes-vault stub.

The author's version appends to an Obsidian daily note. This version writes
plain markdown files under config.DAILY_NOTE_DIR / config.SOCIAL_DIGEST_DIR
so the scripts have somewhere real to write without requiring any particular
notes app. Replace with your own integration (Obsidian, Notion, a plain
journal file, whatever) by matching this module's four functions.
"""
from datetime import date, datetime
from pathlib import Path

import config

FRIENDS_DIR = config.SOCIAL_DIGEST_DIR


def today_note_path() -> Path:
    config.DAILY_NOTE_DIR.mkdir(parents=True, exist_ok=True)
    return config.DAILY_NOTE_DIR / f"{date.today().isoformat()}.md"


def append_to_daily_note(heading: str, content: str) -> bool:
    """Append `content` under `heading` in today's note. Returns False (and
    skips writing) if that heading already has content today, matching the
    idempotent "don't double-post" behavior the real vault helper has."""
    path = today_note_path()
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if heading in existing:
        return False
    block = f"\n{heading}\n{content}\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(block)
    return True


def most_recent_social_digest() -> tuple[Path | None, float | None]:
    """Returns (path, age_in_hours) for the newest digest file, or (None, None)."""
    FRIENDS_DIR.mkdir(parents=True, exist_ok=True)
    digests = sorted(FRIENDS_DIR.glob("social_digest_*.md"))
    if not digests:
        return None, None
    latest = digests[-1]
    age_hours = (datetime.now().timestamp() - latest.stat().st_mtime) / 3600
    return latest, age_hours
