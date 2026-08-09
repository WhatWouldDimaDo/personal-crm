"""Minimal job-lock stub.

The author's version is a launchd-aware file lock that prevents overlapping
runs of the same scheduled job. This version is a simple advisory file lock
— good enough for local testing, not battle-tested for real scheduling.
Supports both call styles used by the job scripts: `lock.acquire()` /
`lock.release()`, and `with JobLock("name"):`.
"""
import os
import time
from pathlib import Path

import config

# A lock file older than this is treated as stale (left behind by a crashed
# run) rather than a live lock, so a dead process doesn't block forever.
STALE_AFTER_HOURS = 6


class JobLock:
    def __init__(self, name: str):
        self.name = name
        self.path = config.OUTPUT_DIR / f".{name}.lock"

    def _is_stale(self) -> bool:
        try:
            age_hours = (time.time() - self.path.stat().st_mtime) / 3600
        except FileNotFoundError:
            return False
        if age_hours >= STALE_AFTER_HOURS:
            return True
        try:
            pid = int(self.path.read_text().strip())
        except (ValueError, OSError):
            return False
        try:
            os.kill(pid, 0)  # signal 0: check the PID exists, don't actually signal it
        except ProcessLookupError:
            return True       # recorded PID is dead — stale
        except PermissionError:
            return False      # PID exists (owned by someone else) — treat as live
        return False

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and self._is_stale():
            self.path.unlink(missing_ok=True)  # crashed run's lock — clear it
        try:
            # O_EXCL makes the create-if-absent check and the write a single
            # atomic syscall — no window between "does it exist" and "write
            # it" for a second process to land in.
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        with os.fdopen(fd, "w") as f:
            f.write(str(os.getpid()))
        return True

    def release(self) -> None:
        self.path.unlink(missing_ok=True)

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError(f"{self.name} already running (lock: {self.path})")
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()
        return False
