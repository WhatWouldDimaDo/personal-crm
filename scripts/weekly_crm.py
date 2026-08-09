#!/usr/bin/env python3
"""
weekly_crm.py — Run CRM scan + weekly digest and link to your daily note.

Designed to run daily for the comm_scan refresh, with the full digest +
birthday check reserved for a weekly cadence (e.g. Fridays) in your scheduler
of choice — this script itself just takes an --full flag; wire the schedule
up however you already do cron/launchd/etc.

Usage:
    python3 scripts/weekly_crm.py            # comm_scan + birthday check only
    python3 scripts/weekly_crm.py --full     # also runs weekly_digest.py
"""
import sys
import subprocess
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config
from support.vault import append_to_daily_note, most_recent_social_digest
from support.lock import JobLock
from support.notify import notify_success, notify_failure
from support.imsg import send_self

SCRIPTS_DIR = Path(__file__).resolve().parent
PYTHON = sys.executable


def run_script(script: str, *args, timeout: int = 600):
    cmd = [PYTHON, str(SCRIPTS_DIR / script)] + list(args)
    result = subprocess.run(cmd, cwd=str(SCRIPTS_DIR), capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(f"{script} failed: {result.stderr[:200]}")
    return result.stdout


def birthday_check(today: datetime):
    """7-day heads-up + day-of text, read from the CRM database's `birthday`
    field (MM-DD or YYYY-MM-DD). Sends a self-iMessage (if configured) and
    drops a line in the daily note."""
    import json

    try:
        db = json.loads(config.CRM_DATABASE.read_text())
    except Exception as e:
        print(f"[weekly_crm] birthday check skipped: {e}")
        return

    from datetime import timedelta
    targets = {(today + timedelta(days=d)).strftime("%m-%d"): d for d in (0, 3, 7)}
    hits = []
    for name, row in db.items():
        b = row.get("birthday")
        if not b:
            continue
        mmdd = b[5:] if len(b) == 10 else b   # YYYY-MM-DD -> MM-DD
        if mmdd in targets:
            hits.append((targets[mmdd], name, row.get("phone") or ""))

    if not hits:
        return
    hits.sort()
    lines = ["Birthdays:"]
    for days, name, phone in hits:
        first = name.split()[0]
        if days == 0:
            lines.append(f"TODAY: {name} — \"Happy birthday {first}!! Hope the day treats you right\" {phone}")
        else:
            lines.append(f"{name} in {days}d ({phone})")
    msg = "\n".join(lines)
    send_self(msg)
    append_to_daily_note("## Morning Brief", "> " + msg.replace("\n", "\n> "))
    print(f"[weekly_crm] {len(hits)} birthday alert(s) sent")


def run():
    today = datetime.now()
    full = "--full" in sys.argv

    # Always run comm_scan with CRM writeback
    print("[weekly_crm] Running comm_scan --write-crm...")
    run_script("comm_scan.py", "--write-crm")

    # Birthday engine — daily
    birthday_check(today)

    # Full run: digest + link in daily note
    if full:
        print("[weekly_crm] --full — running weekly_digest.py...")
        run_script("weekly_digest.py")

        # Link digest in daily note
        digest_path, age = most_recent_social_digest()
        if digest_path:
            digest_name = digest_path.stem
            link = f"> [[{digest_name}]] — Social digest ready. See your notes vault."
            append_to_daily_note("## Morning Brief", link)
            notify_success("weekly_crm", f"Social digest ready: {digest_name}")
        else:
            notify_failure("weekly_crm", "Digest file not found after run")
    else:
        print(f"[weekly_crm] {today.strftime('%A')} — comm_scan only (run with --full for the weekly digest)")
        notify_success("weekly_crm", "CRM contacts refreshed")


if __name__ == "__main__":
    with JobLock("weekly_crm"):
        try:
            run()
        except Exception as e:
            notify_failure("weekly_crm", str(e))
            raise
