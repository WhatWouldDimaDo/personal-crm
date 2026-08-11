# Personal CRM technical guide

This repository demonstrates a local relationship-cadence pipeline. It is not a
hosted CRM and should only be pointed at data you are authorized to process.

## Data contracts

- `gc_phone_index.json` is the broad, read-only identity-resolution layer.
- `crm_database.json` is the curated set of people, tiers, notes, handles, and
  communication state.
- `social_activation_engine.md` carries human-readable cadence records.
- `outreach_tracker.json` stores outreach state used by the dashboard.
- iMessage, call-history, and Address Book databases are macOS system inputs;
  they are never sample-replaced unless you explicitly override their paths.

The included `samples/` files demonstrate the schemas. Copy them to a private
location outside the checkout before replacing fictional records with real ones.

## Daily flow

1. `comm_scan.py` resolves handles, reads recent communication timestamps, and
   optionally writes last-contact state back to the CRM and cadence file.
2. `social_scan.py` calculates signed days overdue and a separate tier-weighted
   priority score, then optionally matches overdue people to events.
3. `social_nudge.py` emits a small daily prompt; `crm_dashboard.py` renders a
   local HTML view.
4. `weekly_crm.py` and `weekly_digest.py` orchestrate the steps for a scheduler.

## Privacy and consent boundary

Timestamp analysis is already sensitive. The optional availability detector goes
further: it reads incoming message text from a 30-day window and checks messages
from matched CRM contacts for phrases suggesting availability. It runs locally
and does not retain unmatched content, but local processing does not eliminate the
social consent question. Review or remove that feature before enabling writeback.

## Known gotchas

- **The first `comm_scan.py` run can touch real system databases.** Export the
  sandbox variables from the README before testing.
- **Do not use SQLite `immutable=1` as the primary Messages read.** It ignores the
  WAL and can silently miss recent messages. The code tries ordinary read-only
  mode first and uses immutable mode only as a fallback.
- **Full Disk Access differs between Terminal and launchd.** Test from the same
  responsible process that will run the scheduled job.
- **`weekly_crm.py --full` writes state.** It is not a sample walkthrough and
  will refuse repository-local write targets.
- **`daysOverdue` and `priorityScore` are different units.** The former is signed
  calendar days; the latter multiplies overdue days by a tier weight.
- **Household links share a cadence clock.** Contact with either linked person can
  reset the relationship clock; decide whether that matches your model.
- **Identity matching is probabilistic plumbing.** Normalize country codes and
  aliases, inspect ambiguous matches, and do not assume every handle maps cleanly.
- **Generated HTML and JSON are private data.** The default output directory is
  outside the repo, but changing `CRM_OUTPUT_DIR` can weaken that boundary.
- **The integration helpers are stubs.** Notification, vault, and self-iMessage
  behavior must be adapted and tested for your environment.

## Safe adoption sequence

1. Run every command with the system DBs pointed at nonexistent sandbox paths.
2. Copy sample schemas to a private directory and set all data/output variables.
3. Enable read-only communication scanning and manually review matches.
4. Decide whether message-content availability detection is appropriate.
5. Enable writeback only after backing up the private CRM files.
6. Run the orchestrator manually from the eventual scheduler context before
   installing a recurring job.

Never commit a real CRM file and rely on a later `.gitignore` change to remove
it; Git history retains previously committed content.
