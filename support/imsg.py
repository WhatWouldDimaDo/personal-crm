"""Minimal self-iMessage stub, used only for a note-to-self nudge (birthdays,
overdue-contact alerts). Sends nothing unless config.SELF_PHONE is set — by
default it just prints what it would have sent.

This is a deliberately thin wrapper. If you want the real thing on macOS:
find your own chat's guid in `~/Library/Messages/chat.db` and send via
`osascript -e 'tell application "Messages" to send "..." to chat id "..."'`.
See social_nudge.py's send_nudge_to_phone() for a worked example, and the
README's TCC/launchd callout for why this needs to run from something that
holds Full Disk Access (Terminal.app), not directly from a launchd job.
"""
import subprocess
import sys

import config


def send_self(message: str) -> bool:
    if not config.SELF_PHONE:
        print(f"[imsg] SELF_PHONE not configured — would send:\n{message}", file=sys.stderr)
        return False
    safe = message.replace("\\", "\\\\").replace('"', '\\"')
    script = f"""tell application "Messages"
    send "{safe}" to buddy "{config.SELF_PHONE}" of (service 1 whose service type is iMessage)
end tell"""
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=15)
        return r.returncode == 0
    except Exception as e:
        print(f"[imsg] send failed: {e}", file=sys.stderr)
        return False
