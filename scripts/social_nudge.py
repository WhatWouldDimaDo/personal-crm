#!/usr/bin/env python3
"""Social Nudge — push overdue contacts + event matches to daily note + notification.

Purpose: close the "nothing pushes" gap — surface who to text and why,
without you having to remember to open a dashboard.

Designed to run on a schedule (e.g. daily midday).
"""
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config
from support.vault import append_to_daily_note
from support.lock import JobLock
from support.notify import notify_success, notify_failure

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
from social_scan import (
    parse_sae_file,
    compute_priority,
    days_overdue_str,
    load_events,
    match_events_to_person,
)

MAX_NUDGES = 5
MAX_NOTIFY = 3


def send_nudge_to_phone(text: str) -> bool:
    """Text the nudge to your own iMessage thread — meet yourself on the
    phone, not just in the daily note. Requires config.SELF_PHONE to be set;
    otherwise this is a no-op (the daily-note write still happens either way).

    Returns False if TCC blocks Apple Events from this context — see the
    README's TCC/launchd callout for why scheduled jobs need to run through
    something holding Full Disk Access (Terminal.app via osascript), not
    directly.
    """
    import sqlite3
    import subprocess

    if not config.SELF_PHONE:
        print("[social_nudge] SELF_PHONE not configured — skipping phone delivery", file=sys.stderr)
        return False

    safe = text.replace("\\", "\\\\").replace('"', '\\"')
    # guid-addressed send — the "1st account whose service type" query style
    # can hang on some setups; looking up the chat guid directly is reliable.
    guid = None
    try:
        conn = sqlite3.connect(f"file:{config.IMESSAGE_DB}?mode=ro", uri=True)
        row = conn.execute("SELECT guid FROM chat WHERE chat_identifier = ? ORDER BY ROWID DESC LIMIT 1",
                           (config.SELF_PHONE,)).fetchone()
        conn.close()
        guid = row[0] if row else None
    except Exception:
        pass
    if not guid:
        print("[social_nudge] no self-chat guid found", file=sys.stderr)
        return False
    script = f"""tell application "Messages"
    set c to a reference to chat id "{guid}"
    send "{safe}" to c
end tell"""
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            print(f"[social_nudge] iMessage send failed: {r.stderr.strip()[:200]}", file=sys.stderr)
            return False
        return True
    except Exception as e:
        print(f"[social_nudge] iMessage send error: {e}", file=sys.stderr)
        return False


def run():
    today = date.today()

    if not config.SAE_FILE.exists():
        raise RuntimeError(f"SAE not found: {config.SAE_FILE}")

    sae = parse_sae_file(config.SAE_FILE)
    people = sae["people"]

    # Compute priority for everyone
    for p in people:
        p["_priority"] = compute_priority(p, today)
        p["_overdue_str"] = days_overdue_str(p, today)

    # Get overdue contacts sorted by priority
    # Filter out dormant contacts (>3x cadence = not an active relationship)
    CADENCE_DAYS = {"weekly": 7, "bi-weekly": 14, "monthly": 30, "quarterly": 90, "6 months": 180, "yearly": 365}
    actionable = []
    for p in people:
        if p["_priority"] <= 0:
            continue
        last = p.get("last_contact")
        if not last:
            continue  # skip unknown — not actionable without baseline
        cadence = p.get("cadence", "quarterly")
        max_window = CADENCE_DAYS.get(cadence, 90) * 3
        days_since = (today - last).days
        if days_since <= max_window:
            actionable.append(p)

    # Sort: tier 1 first (inner circle), then by priority within tier
    overdue = sorted(actionable, key=lambda p: (p.get("tier", 3), -p["_priority"]))

    if not overdue:
        notify_success("social_nudge", "No overdue contacts today")
        return

    # Load events for matching (optional — see config.EVENTS_JSON)
    events = []
    if config.EVENTS_JSON.exists():
        events = load_events(config.EVENTS_JSON, today)

    # Build nudge lines
    lines = []
    now = datetime.now()
    lines.append(f"## Social Nudge — {now.strftime('%-I:%M %p')}")
    lines.append("")

    notify_parts = []

    # Assign events once, with variety — each event suggested to at most one
    # person per nudge (avoids "invite everyone to the same show"). A person
    # gets their best not-yet-used match, or no hook if all their matches are taken.
    event_for: dict[str, dict] = {}
    used_titles: set[str] = set()
    if events:
        for p in overdue[:MAX_NUDGES]:
            for m in match_events_to_person(p, events, today):
                ev = m["event"]
                if ev["title"] not in used_titles:
                    event_for[p["name"]] = ev
                    used_titles.add(ev["title"])
                    break

    for i, p in enumerate(overdue[:MAX_NUDGES]):
        name = p["name"]
        first = name.split()[0]
        tier = p.get("tier", 3)
        groups = p.get("groups", [])
        group_str = groups[0] if groups else ""

        event_hook = ""
        ev = event_for.get(name)
        if ev:
            event_hook = f' · "{ev["title"]} {ev.get("dateStr", "")}"'

        # Last contact
        last = p.get("last_contact")
        if last:
            days_since = (today - last).days
            last_str = f"{days_since}d ago ({last.strftime('%b %-d')})"
        else:
            last_str = "unknown"

        line = f"{i+1}. **{name}** — {p['_overdue_str']}"
        if group_str:
            line += f" · {group_str}"
        if event_hook:
            line += event_hook
        lines.append(line)

        if i < MAX_NOTIFY:
            notify_parts.append(f"{first} ({p['_overdue_str']})")

    lines.append("")
    lines.append(f"> Text the top {min(2, len(overdue))} today.")

    content = "\n".join(lines)

    # Write to daily note
    appended = append_to_daily_note("## Social Nudge", content)
    if not appended:
        print("Social Nudge section already in daily note, skipping.", file=sys.stderr)

    # Push notification
    summary = ", ".join(notify_parts)
    notify_success("social_nudge", f"Text: {summary}")

    # iMessage to your own phone — top 3 with event hooks
    phone_lines = ["Social Nudge", ""]
    for p in overdue[:MAX_NOTIFY]:
        hook = ""
        ev = event_for.get(p["name"])
        if ev:
            hook = f' — invite to {ev["title"]} {ev.get("dateStr", "")}'
        phone_lines.append(f'{p["name"]} ({p["_overdue_str"]}){hook}')
    sent = send_nudge_to_phone("\n".join(phone_lines))
    print(f"[social_nudge] phone delivery: {'sent' if sent else 'skipped/failed (see send_nudge_to_phone)'}", file=sys.stderr)


if __name__ == "__main__":
    with JobLock("social_nudge"):
        try:
            run()
        except Exception as e:
            notify_failure("social_nudge", str(e))
            raise
