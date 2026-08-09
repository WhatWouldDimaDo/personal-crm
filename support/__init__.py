"""Integration-point stubs used by the job scripts (weekly_crm.py, social_nudge.py).

In the author's real setup these wrap an Obsidian vault, a launchd job lock,
and a push-notification channel. Here they're minimal working
implementations (a local markdown file, a working advisory file lock, stdout)
so the scripts run standalone. Swap any of them for your own note-taking app,
locking scheme, or notifier — that's the point of keeping them separate from
the CRM logic itself.
"""
