# Social Activation Engine (sample)

A single markdown file acting as a lightweight relational database: a phone
index, per-person slot/group assignments, and cadence-target sections with
rolling `(last: YYYY-MM-DD)` tags that comm_scan.py rewrites in place as it
sees fresh activity. This is what social_scan.py and social_nudge.py
actually read at runtime — the CRM database feeds it, not the reverse.

Fake data throughout. Names match samples/crm_database.sample.json so you
can see the two files cross-reference by name.

## Phone Index

- Jordan Ashby: +15555550101
- Sam Ashby: +15555550102
- Morgan Ellis: +15555550103
- Casey Nguyen: +15555550104
- Riley Chen: +15555550105

## Slot Assignments

| Name | Tier | Kids | best_invite_for | social_groups |
| --- | --- | --- | --- | --- |
| Jordan Ashby | 1 | yes | GROUP_NIGHT, FAMILY_OUT | Concert Squad, Kids Crew |
| Sam Ashby | 1 | yes | DATE_NIGHT, FAMILY_OUT | Couples Dinner, Kids Crew |
| Morgan Ellis | 2 | no | GROUP_NIGHT | Concert Squad |
| Casey Nguyen | 1 | yes | GROUP_NIGHT, SOLO_RESET | Concert Squad |
| Riley Chen | 3 | no | LAST_MINUTE | Close By |

## Cadence Targets

### weekly (1)
- Jordan Ashby (last: 2026-07-30)

### bi-weekly (1)
- Casey Nguyen (last: 2026-07-20)

### monthly (1)
- Morgan Ellis (last: 2026-05-01)

### quarterly (2)
- Sam Ashby (last: 2026-06-14)
- Riley Chen (last: unknown)

## Couples Dinner Pairs

- Jordan Ashby+Sam Ashby
