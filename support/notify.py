"""Minimal notification stub — prints to stdout/stderr.

The author's version pushes to a phone via a notification service. Swap in
your own (a Pushover/ntfy call, a Slack webhook, whatever) by matching these
two function signatures.
"""


def notify_success(job: str, message: str) -> None:
    print(f"[{job}] OK: {message}")


def notify_failure(job: str, message: str) -> None:
    print(f"[{job}] FAILED: {message}")
