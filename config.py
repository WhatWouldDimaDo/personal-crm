"""
Central configuration for the Personal CRM automation scripts.

Every path below can be overridden with an environment variable of the same
name, so you can point the scripts at your own real data instead of the
bundled samples:

    CRM_DATABASE=~/notes/crm.json python3 scripts/comm_scan.py

Defaults point at the fake sample data in samples/ so the scripts run
out of the box with zero configuration. See README.md "Quickstart" for a
walkthrough, and "Data safety" for why real data should never live inside
this repo's working tree.
"""
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent


def _path(env_var: str, default: Path) -> Path:
    override = os.environ.get(env_var)
    return Path(override).expanduser() if override else default


def assert_writable_outside_repo(path: Path, name: str) -> None:
    """Refuse to write real data into a path inside this repo's working
    tree. The bundled samples/*.sample.json files are git-tracked, so
    writing real scan data to their default location would stage a
    forker's real contact data (names, dates, message snippets) for their
    next commit. Call this immediately before any writeback. See README
    "Data safety"."""
    if path.resolve().is_relative_to(REPO_ROOT):
        sys.exit(
            f"Refusing to write real data into the repo tree ({name}={path}) — set "
            f"{name} to a path outside this repo (see README: Data safety)."
        )


# --- The three-layer system -------------------------------------------------
# Phone master: everyone, shallow. Curated CRM: close people, deep. Cadence
# engine (SAE): who's overdue, computed from the other two. See README
# "Three-layer design".

CRM_DATABASE = _path("CRM_DATABASE", REPO_ROOT / "samples" / "crm_database.sample.json")
GC_PHONE_INDEX = _path("GC_PHONE_INDEX", REPO_ROOT / "samples" / "gc_phone_index.sample.json")
SAE_FILE = _path("SAE_FILE", REPO_ROOT / "samples" / "social_activation_engine.sample.md")
OUTREACH_TRACKER = _path("OUTREACH_TRACKER", REPO_ROOT / "samples" / "outreach_tracker.sample.json")

# Optional: a JSON export of upcoming events shaped like a list of
# {id, title, date, dateStr, venue, score, slots, ticketUrl, officialUrl,
# urgent, tier} objects. Event sourcing/enrichment is out of scope for this
# repo — bring your own. When absent, social_scan.py's event-matching
# sections are skipped rather than crashing.
EVENTS_JSON = _path("EVENTS_JSON", REPO_ROOT / "samples" / "events.sample.json")

# --- macOS system sources ----------------------------------------------------
# These point at YOUR real local databases. There is no sample/fake version
# of these — the whole point is they read your own machine, locally, and
# nothing here ever ships that data anywhere.

IMESSAGE_DB = _path("IMESSAGE_DB", Path.home() / "Library/Messages/chat.db")
CALL_HISTORY_DB = _path(
    "CALL_HISTORY_DB",
    Path.home() / "Library/Application Support/CallHistoryDB/CallHistory.storedata",
)


def _addressbook_sources() -> list[Path]:
    """Apple Contacts (AddressBook) source databases, checked in order — the
    one with the most phone records wins. macOS keeps one or more source
    folders under Sources/ with machine-generated UUID names (not meaningful,
    not secret, just noise) — glob for them instead of hardcoding any.

    Override with ADDRESSBOOK_DIR to point at a different (e.g. empty/test)
    directory instead of your real ~/Library/Application Support/AddressBook
    — useful for exercising the "AddressBook unavailable" path deliberately.
    """
    base = Path(os.environ.get("ADDRESSBOOK_DIR", "")).expanduser() if os.environ.get("ADDRESSBOOK_DIR") \
        else Path.home() / "Library/Application Support/AddressBook"
    sources_dir = base / "Sources"
    found = sorted(sources_dir.glob("*/AddressBook-v22.abcddb")) if sources_dir.exists() else []
    found.append(base / "AddressBook-v22.abcddb")  # older single-source layout
    return found


ADDRESSBOOK_SOURCES = _addressbook_sources()

# --- Output / working directory ----------------------------------------------

# Generated artifacts default OUTSIDE the repo. They carry real data — matched
# names, relationship notes, and handles of people who aren't even in the CRM —
# and defaulting them to <repo>/output/ left `.gitignore` as the only thing
# between a forker and committing all of it. Removing the hazard beats guarding
# it: nothing this tool generates lands in the working tree unless you ask.
OUTPUT_DIR = _path("CRM_OUTPUT_DIR", Path.home() / ".local" / "share" / "personal-crm")
DASHBOARD_HTML = _path("DASHBOARD_HTML", OUTPUT_DIR / "crm_dashboard.html")
DAILY_NOTE_DIR = _path("DAILY_NOTE_DIR", OUTPUT_DIR / "daily-notes")
SOCIAL_DIGEST_DIR = _path("SOCIAL_DIGEST_DIR", OUTPUT_DIR / "digests")

# --- Self-identification ------------------------------------------------------
# Used only by social_nudge.py to address a self-iMessage (a note-to-self
# nudge). Leave unset to disable that delivery channel — the nudge still
# writes to the daily note either way.
SELF_PHONE = os.environ.get("SELF_PHONE", "")

# --- Inner circle (social_scan.py) --------------------------------------------
# "Always invite" people per social group. This is inherently personal data,
# so it ships empty — populate it yourself via INNER_CIRCLE_FILE pointing at
# a JSON file shaped like:
#   {"Group Name": ["Full Name", "Full Name"]}


def _load_inner_circle() -> dict[str, set[str]]:
    path = os.environ.get("INNER_CIRCLE_FILE")
    if not path:
        return {}
    try:
        with open(Path(path).expanduser()) as f:
            raw = json.load(f)
        return {group: set(names) for group, names in raw.items()}
    except Exception:
        return {}


INNER_CIRCLE = _load_inner_circle()
