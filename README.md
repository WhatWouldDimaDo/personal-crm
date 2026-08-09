# Personal CRM

I built this because I kept losing track of who I hadn't talked to in a while
— not strangers, close friends. The tools that exist for this are either
built for salespeople or ask you to re-enter your entire social graph by
hand. This is neither. It's a small stack of scripts that reads my own phone
and turns "who am I forgetting" into a list, without me having to remember
to check.

The core idea: keep a curated list of people I actually care about staying
close to, read my own iMessage and call history to see who I've actually
talked to, and compute who's overdue against a cadence I set per person (a
close friend might be "weekly," someone I see twice a year might be
"6-month"). Everything runs locally, on a schedule, and drops the result
somewhere I'll actually see it — a dashboard, a note, a text to myself.

This repo is the sanitized, reference version: the real automation logic,
with every path parameterized and every scrap of my actual data stripped
out. Point it at your own CRM file and it's yours. Point it at nothing and
it runs against the fake sample data in `samples/` so you can see the shape
of it before committing to anything.

## Architecture

Three layers, glued together by two data files, kept fresh by a daily job
chain:

```
chat.db (iMessage) ─┐
CallHistory.storedata ┴─► comm_scan.py ──► crm_database.json (writeback: last_contact_date,
                                    │                          last_platform, contact_count_90d)
                                    ├────────────────────────► social_activation_engine.md
                                    │                          (advances "last:" cadence dates)
                                    └────────────────────────► comm_scan_results.json (per-run cache)

Phone export (e.g. Google Contacts) ► gc_phone_index.json (fallback resolution layer, read-only
                                                             input to comm_scan.py)

crm_database.json + SAE ─────────────► social_scan.py ──► social_digest_*.md (your notes vault)
                                              │
                                              └──────────► social_nudge.py ──► daily note + phone
                                                                                push + self-iMessage

crm_database.json + outreach_tracker.json + SAE ─► crm_dashboard.py ──► crm_dashboard.html
```

`weekly_digest.py` and `weekly_crm.py` are the orchestrators — they chain the
above into one command for a scheduler (cron, launchd, whatever) to call.

## The three-layer design

**Phone master** (`gc_phone_index.json`) — everyone. A flat export from your
phone/contacts, a few thousand entries, no tier, no notes. Just enough to
resolve a raw phone number or email to a name. This is the fallback layer:
if someone texts you and they're not in your curated CRM, you still get
their name instead of a bare number.

**Curated CRM** (`crm_database.json`) — the people who matter. Maybe a
hundred, keyed by full name, with a tier (1–3), a cadence target, kids,
birthdays, spouse links, and whatever notes are useful to have on hand. This
is the layer you maintain by hand; everything else derives from it or writes
back into it.

**Cadence engine** (`social_activation_engine.md`) — who's overdue, and by
how much. A markdown file that doubles as a lightweight database: a phone
index, a slot-assignment table (who's good for a group night vs. a family
outing vs. a one-on-one), and cadence sections that track a rolling `last:`
date per person. `comm_scan.py` advances those dates as it sees real
activity; `social_scan.py` reads them to compute an overdue score
(`days_overdue × tier_multiplier`) and match overdue people to upcoming
events worth inviting them to.

Everything reduces to one sortable number. That's the whole trick — instead
of trying to remember 100 different relationships' worth of context, the
system turns "have I kept up with this person" into a single overdue count,
and a spouse/partner link means contacting either half of a couple resets
the shared clock, so I'm not double-penalized for texting one partner more
than the other.

## Two things I learned the hard way

> **SQLite WAL vs. `immutable=1`.** Reading `chat.db` read-only has two
> modes: `file:chat.db?mode=ro` and `file:chat.db?mode=ro&immutable=1`. The
> `immutable=1` flag is tempting for a scheduled job — it skips SQLite's
> locking machinery entirely — but it also makes SQLite **ignore the WAL
> file**, so any message still sitting in `chat.db-wal` (i.e. anything
> recent that hasn't been checkpointed into the main database yet) is
> invisible. This silently froze "last contact" at up to two weeks stale for
> every person I was tracking before I caught it. The fix: always try plain
> `mode=ro` first, and only fall back to `immutable=1` if that connection
> fails outright. If you find this exact snippet on a blog somewhere with
> `immutable=1` hardcoded, that's this bug waiting to happen to you too.

> **The TCC / launchd / Full Disk Access maze.** A scheduled (launchd) job
> can't hold Full Disk Access itself — macOS always treats a launchd-spawned
> process as its own "responsible process" for privacy purposes, and FDA
> can't be granted to launchd directly, only to specific apps. The pattern
> that actually works: have the scheduler trigger `osascript` that opens or
> targets Terminal.app, which *does* hold FDA — that makes Terminal the
> responsible process for the `chat.db` read, not the launchd job. This is a
> reusable pattern for any macOS background job that needs iMessage,
> Contacts, or Calendar access — worth knowing before you spend an afternoon
> confused about why a script that works fine in your terminal silently
> returns nothing when launchd runs it.

## Quickstart

Stdlib-only Python 3.11+, nothing to `pip install`. The CRM/SAE/phone-index
paths default to the fake data in `samples/`, so most of this runs safely
out of the box. One thing to know: `comm_scan.py` defaults `IMESSAGE_DB` /
`CALL_HISTORY_DB` / the AddressBook lookup to **your real, live paths** —
`~/Library/Messages/chat.db` and friends — on the theory that reading your
own phone is the entire point (see "Requirements" below). On a real Mac with
a real chat.db, that means the plain `--dry-run` command below will print
*your actual contacts* to your terminal, not fake ones. That's intentional
once you're using this for real, but if you just want to see the sample-data
flow first without touching anything real, point those three at somewhere
empty:

```bash
git clone <this-repo>
cd personal-crm

# Sandbox the system-DB paths so this run touches only samples/, not your
# real phone — useful for a first look, or for CI.
export IMESSAGE_DB=/tmp/no-such-chat.db
export CALL_HISTORY_DB=/tmp/no-such-callhistory.db
export ADDRESSBOOK_DIR=/tmp/no-such-addressbook

# Comm scan — with the DBs sandboxed above, this exercises the "DB not
# accessible" path cleanly: a warning per source, zero matches, no crash.
python3 scripts/comm_scan.py --dry-run

# Cadence engine + event matching against the sample SAE + sample events.
# --no-cal skips the optional Google Calendar slot-gating lookup (via the
# `gws` CLI) so this stays fully offline for the sample walkthrough too.
python3 scripts/social_scan.py --no-cal

# First-names-only roster export (the only artifact meant to ever leave
# your machine, e.g. for a public site widget)
python3 scripts/social_scan.py --export-roster

# Dashboard — writes ~/.local/share/personal-crm/crm_dashboard.html
# (override the location with CRM_OUTPUT_DIR; see "Data safety")
python3 scripts/crm_dashboard.py

# Nudge — computes overdue people, writes a stub daily note under
# ~/.local/share/personal-crm/daily-notes/
python3 scripts/social_nudge.py
```

`weekly_crm.py --full` is NOT a sample-data command — it always runs
`comm_scan.py --write-crm`, which writes back to `config.CRM_DATABASE` /
`config.SAE_FILE`. Only run it after you've pointed those env vars at your
own files outside this repo (see below); running it against the defaults
will hit the refusal in `config.assert_writable_outside_repo()`.

When you're ready to point this at your own real data, unset those three
(or just open a new shell) so `comm_scan.py` falls back to your actual
`chat.db`, and set the CRM/SAE/phone-index paths from `config.py` at your
own private files — anywhere outside this repo's working tree:

```bash
CRM_DATABASE=~/notes/crm.json \
GC_PHONE_INDEX=~/notes/phone-index.json \
SAE_FILE=~/notes/cadence.md \
python3 scripts/comm_scan.py --write-crm
```

With those same env vars exported for the session (not just prefixed on one
command), `weekly_crm.py --full` runs the full daily/weekly chain —
comm_scan writeback, birthday check, and (on the days you pass `--full`)
the social digest:

```bash
export CRM_DATABASE=~/notes/crm.json
export GC_PHONE_INDEX=~/notes/phone-index.json
export SAE_FILE=~/notes/cadence.md
export OUTREACH_TRACKER=~/notes/outreach.json
python3 scripts/weekly_crm.py --full
```

Each roster entry (`build_roster()` in `social_scan.py`) carries `daysOverdue`
— real calendar days, signed (negative = not yet due, matching the "Nd until
due" phrasing in `overdueStr`), `null` when last contact is unknown — and,
separately, `priorityScore`, the tier-weighted ranking number
(`days_overdue × tier_multiplier`) the roster is actually sorted by. Don't
publish `priorityScore` as if it were a day count — it isn't one.

## Requirements — the honest version

- **macOS only.** The iMessage and call-history reads are Apple-specific
  SQLite databases; there's no cross-platform version of this.
- **Full Disk Access**, granted to whatever holds it in your setup (Terminal
  by default — see the TCC callout above for why launchd jobs need the
  extra hop).
- **It reads your own `chat.db` and call history, locally, on your own
  machine.** Nothing is uploaded anywhere. There's no server component, no
  API calls except the optional Google Calendar lookup you can just not
  configure. The output is a JSON file and an HTML dashboard that live on
  disk.
- **No sample/fake substitute for the system databases.** Everything else in
  this repo can run against fake data; `chat.db` and call history are always
  yours or not accessed at all.

## A feature that needs your consent, not just your code

`comm_scan.py` includes an availability scanner: it reads the *content* of
incoming messages (not just timestamps) from people already in your CRM,
looking for phrases like "just got back" or "free this weekend," and flags
them as available for a rolling 14 days. It's genuinely useful — it's the
difference between guessing when to reach out and knowing someone actually
signaled being free — but it's also the one piece of this system that reads
what people wrote to you, not just when.

A few things make it worth turning on rather than something to be uneasy
about: the 30-day-lookback SQL query does pull every incoming message's text
in that window (that's how SQLite works — it can't pre-filter by "is this
sender in my CRM" without a per-handle allowlist), but only messages from
handles already matched to your own curated CRM ever get checked against the
availability patterns or held onto past that check — everything else is
discarded in the same loop iteration, unmatched and unstored. It looks back
30 days at most, every flag expires after 14 days on its own, and it runs
entirely on your machine — nothing about a message's content is ever sent
anywhere. It's opt-in: it only runs when you pass `--write-crm`, and the
patterns it matches on are plain regex in `comm_scan.py` you can read,
edit, or delete outright.

Use your judgment about the people in your life, not just the code — this
reads texts from people who didn't necessarily expect that, even if the
"reading" is local and the "people" are your own closest friends.

## Data safety

None of your real data belongs in this repo's working tree. `.gitignore`
blocks the obvious real-data filenames (`crm_database.json`,
`gc_phone_index.json`, `social_activation_engine.md`, `outreach_tracker.json`,
any `*_results.json` or rendered `crm_dashboard.html`) so an accidental
`git add .` doesn't ship them — but that's a convenience, not the safeguard,
and it does nothing for the `samples/*.sample.json` / `samples/*.sample.md`
files themselves, which are tracked on purpose (they're fake data meant to
ship with the repo). Two things do the real work:

**Generated output lands outside the repo by default.** Everything the
scripts produce — scan results, run logs, the rendered dashboard, digests,
daily notes — is written under `~/.local/share/personal-crm/` (override with
`CRM_OUTPUT_DIR`). Those artifacts carry matched names, relationship notes,
and the raw handles of people who aren't in your CRM at all, so they don't
belong in a working tree you might `git add`. Nothing this tool generates
touches the repo unless you point it there yourself.

**Writeback into the repo is refused at runtime.**
`config.assert_writable_outside_repo()` exits with an error naming the env
var to change if `CRM_DATABASE`, `SAE_FILE`, or `OUTREACH_TRACKER` resolves
inside this repo — which is what the shipped defaults do, so a real run
fails loudly instead of quietly staging your contacts. Point those env vars
at files *outside* the repo (a notes vault, a private folder, wherever) and
the writeback paths work normally. The check covers those three inputs
specifically; it is not a general filesystem guard.

If you fork this to build your own version with real data, start a fresh
git history for your fork's data-holding branch, or better, never let real
data touch a directory that's ever been `git add`ed. History doesn't forget
— a `.gitignore` entry added after the fact does nothing for commits that
already happened.

## Layout

```
config.py                          # all paths, env-var overridable, documented defaults
scripts/
  comm_scan.py                     # reads chat.db + call history, writes CRM/SAE back
  social_scan.py                   # cadence engine + event matching + brief generation
  weekly_digest.py                 # orchestrates comm_scan -> social_scan -> vault write
  crm_dashboard.py                 # renders a static self-contained HTML dashboard
  social_nudge.py                  # daily overdue-contact nudge (note + phone push)
  weekly_crm.py                    # top-level scheduler entrypoint
support/
  vault.py, lock.py, notify.py, imsg.py   # integration-point stubs — swap for your own
samples/                           # fake data matching the real schemas, safe to commit
```
