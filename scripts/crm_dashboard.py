#!/usr/bin/env python3
"""
crm_dashboard.py — Phone-first CRM dashboard: whose turn, overdue by tier, birthdays.

Designed to run on a schedule shortly after your outreach-status refresh, so
it reads fresh turn-state. Pure file reads — no chat.db, no iMessage send, no
Terminal wrapper needed (contrast with comm_scan.py / social_nudge.py, which
do need Full Disk Access — see README).

Reads (all paths from config.py):
  - outreach_tracker.json      (curated whose-turn threads)
  - crm_database.json          (last_contact_date, spouse links, birthdays)
  - social_activation_engine.md (tier + cadence table, feeds Overdue-by-Tier)

Writes:
  - config.DASHBOARD_HTML (self-contained, no external assets)

Usage:
    python3 crm_dashboard.py
"""
import json
import re
import sys
from datetime import date, datetime, timedelta
from html import escape
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config

try:
    from support.lock import JobLock
    from support.notify import notify_failure, notify_success
except Exception:  # pragma: no cover - keep the generator runnable standalone
    JobLock = None

    def notify_failure(*a, **k):
        pass

    def notify_success(*a, **k):
        pass

OUTREACH_TRACKER = config.OUTREACH_TRACKER
CRM_DB = config.CRM_DATABASE
SAE_FILE = config.SAE_FILE
OUT_HTML = config.DASHBOARD_HTML

CADENCE_DAYS = {
    "weekly": 7,
    "bi-weekly": 14,
    "monthly": 30,
    "quarterly": 90,
    "6 months": 180,
    "yearly": 365,
}

BIRTHDAY_WINDOW_DAYS = 14
TURN_CAVEAT = (
    "turn-state has known false positives — spot-check the thread before drafting"
)


# --- loaders -----------------------------------------------------------

def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def load_crm() -> dict:
    return load_json(CRM_DB, {})


def load_tracker() -> list:
    data = load_json(OUTREACH_TRACKER, {})
    return data.get("threads", [])


def parse_sae(text: str):
    """Returns (tier_by_name, cadence_by_name) from social_activation_engine.md."""
    tier_by_name = {}
    for line in text.splitlines():
        m = re.match(r"^\|\s*([^|]+?)\s*\|\s*([123])\s*\|", line)
        if m:
            tier_by_name[m.group(1).strip()] = int(m.group(2))

    cadence_by_name = {}
    section_re = re.compile(r"^### ([\w -]+?) \(\d+\)\s*$")
    current = None
    for line in text.splitlines():
        sm = section_re.match(line)
        if sm:
            current = sm.group(1).strip()
            continue
        if line.startswith("## "):
            current = None
            continue
        if current and current in CADENCE_DAYS:
            bm = re.match(r"^- (.+?) \(last: (\d{4}-\d{2}-\d{2})\)\s*$", line)
            if bm:
                cadence_by_name[bm.group(1).strip()] = {
                    "cadence": current,
                    "sae_last": bm.group(2),
                }
    return tier_by_name, cadence_by_name


def load_sae():
    if not SAE_FILE.exists():
        return {}, {}
    return parse_sae(SAE_FILE.read_text())


# --- date helpers --------------------------------------------------------

def parse_date(s: str | None):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def days_since(d) -> int | None:
    if d is None:
        return None
    return (date.today() - d).days


# --- section 1: whose turn ------------------------------------------------

def build_turn_sections(tracker: list):
    your_turn, waiting = [], []
    for t in tracker:
        if t.get("stage") == "met":
            continue
        row = {
            "name": t.get("name", ""),
            "type": t.get("type", ""),
            "stage": t.get("stage", ""),
            "days": days_since(parse_date(t.get("last_message_date"))),
            "next_action": t.get("next_action", ""),
            "context": t.get("context", ""),
        }
        if t.get("last_sender") == "them":
            your_turn.append(row)
        else:
            waiting.append(row)
    your_turn.sort(key=lambda r: r["days"] if r["days"] is not None else -1, reverse=True)
    waiting.sort(key=lambda r: r["days"] if r["days"] is not None else -1, reverse=True)
    return your_turn, waiting


# --- section 2: overdue by tier -------------------------------------------

def build_overdue(crm: dict, tier_by_name: dict, cadence_by_name: dict):
    """Household-effective overdue calc: if either spouse was contacted more
    recently, the household clock resets to that later date."""
    rows_by_tier = {1: [], 2: [], 3: []}
    today = date.today()

    for name, cad_info in cadence_by_name.items():
        tier = tier_by_name.get(name)
        if tier is None:
            continue
        cadence = cad_info["cadence"]
        cadence_days = CADENCE_DAYS[cadence]

        crm_entry = crm.get(name, {})
        eff_date = parse_date(crm_entry.get("last_contact_date")) or parse_date(cad_info["sae_last"])
        reset_by_spouse = False

        spouse_name = crm_entry.get("spouse")
        if spouse_name and spouse_name in crm:
            spouse_last = parse_date(crm[spouse_name].get("last_contact_date"))
            if spouse_last and (eff_date is None or spouse_last > eff_date):
                eff_date = spouse_last
                reset_by_spouse = True

        if eff_date is None:
            continue

        overdue_days = (today - eff_date).days - cadence_days
        if overdue_days <= 0:
            continue

        rows_by_tier[tier].append({
            "name": name,
            "cadence": cadence,
            "last_contact": eff_date.isoformat(),
            "overdue_days": overdue_days,
            "reset_by_spouse": reset_by_spouse,
            "spouse": spouse_name or "",
        })

    for tier in rows_by_tier:
        rows_by_tier[tier].sort(key=lambda r: r["overdue_days"], reverse=True)
    return rows_by_tier


# --- section 3: birthdays -------------------------------------------------

def next_birthday(bday_str: str, today: date):
    parts = bday_str.split("-")
    if len(parts) not in (2, 3):
        return None
    try:
        month, day = int(parts[-2]), int(parts[-1])
        has_year = len(parts) == 3
        birth_year = int(parts[0]) if has_year else None
    except ValueError:
        return None

    try:
        this_year = date(today.year, month, day)
    except ValueError:
        return None  # e.g. Feb 29 on a non-leap year, skip rather than crash

    next_date = this_year if this_year >= today else date(today.year + 1, month, day)
    turning = (next_date.year - birth_year) if birth_year else None
    return next_date, turning


def build_birthdays(crm: dict):
    today = date.today()
    cutoff = today + timedelta(days=BIRTHDAY_WINDOW_DAYS)
    rows = []
    for name, info in crm.items():
        bday = info.get("birthday")
        if not bday:
            continue
        result = next_birthday(bday, today)
        if not result:
            continue
        next_date, turning = result
        if today <= next_date <= cutoff:
            rows.append({
                "name": name,
                "date": next_date.isoformat(),
                "days_out": (next_date - today).days,
                "turning": turning,
            })
    rows.sort(key=lambda r: r["date"])
    return rows


# --- freshness -------------------------------------------------------------

def mtime_str(path: Path) -> str:
    if not path.exists():
        return "never (file not found)"
    dt = datetime.fromtimestamp(path.stat().st_mtime)
    hours_ago = (datetime.now() - dt).total_seconds() / 3600
    age = f"{hours_ago:.1f}h ago" if hours_ago < 48 else f"{hours_ago / 24:.1f}d ago"
    return f"{dt.strftime('%a %b %-d, %-I:%M %p')} ({age})"


# --- HTML rendering ----------------------------------------------------

STAGE_LABEL = {
    "identified": "IDENTIFIED",
    "invited": "INVITED",
    "scheduled": "SCHEDULED",
    "met": "MET",
    "dormant": "DORMANT",
}
TYPE_LABEL = {"social": "SOCIAL", "professional": "PRO"}
TIER_LABEL = {1: "Tier 1 — Closest", 2: "Tier 2 — Extended", 3: "Tier 3 — Dormant"}


def chip(text: str, tone: str = "ink-soft") -> str:
    return f'<span class="chip chip-{tone}">{escape(text)}</span>'


def render_turn_row(r: dict) -> str:
    days = f"{r['days']}d" if r["days"] is not None else "—"
    type_tone = "brass" if r["type"] == "professional" else "sage"
    parts = [
        '<div class="row">',
        '<div class="row-head">',
        f'<span class="row-name">{escape(r["name"])}</span>',
        chip(TYPE_LABEL.get(r["type"], r["type"].upper()), type_tone),
        chip(STAGE_LABEL.get(r["stage"], r["stage"].upper())),
        f'<span class="row-days">{days} in state</span>',
        "</div>",
    ]
    if r.get("next_action"):
        parts.append(f'<p class="row-detail"><strong>Next:</strong> {escape(r["next_action"])}</p>')
    if r.get("context"):
        parts.append(f'<p class="row-detail row-context">{escape(r["context"])}</p>')
    parts.append("</div>")
    return "\n".join(parts)


def render_turn_section(your_turn: list, waiting: list) -> str:
    def group(title: str, rows: list, open_attr: str) -> str:
        body = "\n".join(render_turn_row(r) for r in rows) if rows else '<p class="empty">None right now.</p>'
        return (
            f'<details class="group" {open_attr}>'
            f'<summary><span class="group-title">{escape(title)}</span>'
            f'<span class="group-count">{len(rows)}</span></summary>'
            f'<div class="group-body">{body}</div></details>'
        )

    return (
        '<section class="section">'
        '<div class="section-head"><p class="eyebrow">Outreach</p>'
        '<h2 class="section-title">Whose Turn</h2></div>'
        f'<p class="caveat">{escape(TURN_CAVEAT)}</p>'
        + group("Your Turn", your_turn, "open")
        + group("Waiting on Them", waiting, "")
        + "</section>"
    )


def render_overdue_row(r: dict) -> str:
    note = ""
    if r["reset_by_spouse"]:
        note = f'<p class="row-detail">Household clock reset by {escape(r["spouse"])}\'s more recent contact.</p>'
    return (
        '<div class="row">'
        '<div class="row-head">'
        f'<span class="row-name">{escape(r["name"])}</span>'
        + chip(r["cadence"].upper())
        + f'<span class="row-days">{r["overdue_days"]}d overdue</span>'
        "</div>"
        f'<p class="row-detail">Last contact (household-effective): {escape(r["last_contact"])}</p>'
        + note
        + "</div>"
    )


def render_overdue_section(rows_by_tier: dict) -> str:
    total = sum(len(v) for v in rows_by_tier.values())
    groups = []
    for tier in (1, 2, 3):
        rows = rows_by_tier.get(tier, [])
        body = "\n".join(render_overdue_row(r) for r in rows) if rows else '<p class="empty">None overdue.</p>'
        open_attr = "open" if tier == 1 else ""
        groups.append(
            f'<details class="group" {open_attr}>'
            f'<summary><span class="group-title">{escape(TIER_LABEL[tier])}</span>'
            f'<span class="group-count">{len(rows)}</span></summary>'
            f'<div class="group-body">{body}</div></details>'
        )
    return (
        '<section class="section">'
        '<div class="section-head"><p class="eyebrow">Cadence</p>'
        f'<h2 class="section-title">Overdue by Tier ({total})</h2></div>'
        '<p class="caveat">Household-effective: contacting either partner resets the shared clock.</p>'
        + "".join(groups)
        + "</section>"
    )


def render_birthday_row(r: dict) -> str:
    turning = f", turning {r['turning']}" if r["turning"] else ""
    when = "Today" if r["days_out"] == 0 else ("Tomorrow" if r["days_out"] == 1 else f"in {r['days_out']}d")
    return (
        '<div class="row">'
        '<div class="row-head">'
        f'<span class="row-name">{escape(r["name"])}</span>'
        f'<span class="row-days">{r["date"]} · {when}{turning}</span>'
        "</div></div>"
    )


def render_birthday_section(rows: list) -> str:
    body = "\n".join(render_birthday_row(r) for r in rows) if rows else '<p class="empty">None in the next 14 days.</p>'
    return (
        '<section class="section">'
        '<div class="section-head"><p class="eyebrow">Calendar</p>'
        f'<h2 class="section-title">Birthdays — Next 14 Days ({len(rows)})</h2></div>'
        f'<div class="group-body no-collapse">{body}</div>'
        "</section>"
    )


def render_stat_tile(label: str, count: int) -> str:
    return f'<div class="stat"><span class="stat-count">{count}</span><span class="stat-label">{escape(label)}</span></div>'


def render_footer() -> str:
    rows = [
        ("Outreach turn-state (outreach_tracker.json)", mtime_str(OUTREACH_TRACKER)),
        ("CRM / comm_scan (crm_database.json)", mtime_str(CRM_DB)),
        ("This dashboard, generated", mtime_str(OUT_HTML) if OUT_HTML.exists() else "now"),
    ]
    items = "".join(f"<li><span>{escape(k)}</span><span>{escape(v)}</span></li>" for k, v in rows)
    return (
        '<footer class="footer">'
        '<p class="eyebrow">Freshness</p>'
        f'<ul class="freshness">{items}</ul>'
        "</footer>"
    )


PAGE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CRM Dashboard</title>
<style>
:root {{
  --cream:#f4efe3; --paper:#faf7ef; --ink:#211f1a; --ink-soft:#4a463d;
  --pine:#1e3226; --pine-deep:#16241b; --sage:#77816c;
  --brass:#a9803f; --brass-light:#c9a562;
  --line:rgba(33,31,26,.14);
  --font-display:"Iowan Old Style","Palatino Linotype",Palatino,Georgia,serif;
  --font-body:-apple-system,BlinkMacSystemFont,"Segoe UI","Helvetica Neue",Arial,sans-serif;
}}
* {{ box-sizing:border-box; }}
body {{
  margin:0; background:var(--cream); color:var(--ink);
  font-family:var(--font-body); font-size:0.92rem; line-height:1.5;
  padding-bottom:3rem;
}}
.wrap {{ max-width:640px; margin:0 auto; padding:1.25rem 1rem 2rem; }}
header.top {{ padding:0.5rem 0 1.25rem; }}
header.top .eyebrow {{ margin:0 0 0.35rem; }}
header.top h1 {{
  font-family:var(--font-display); font-weight:400; margin:0;
  font-size:clamp(1.6rem,6vw,2rem); color:var(--pine-deep);
}}
header.top .sub {{ color:var(--ink-soft); font-size:0.82rem; margin:0.4rem 0 0; }}
.eyebrow {{
  font-size:0.7rem; text-transform:uppercase; letter-spacing:.2em;
  color:var(--brass); font-weight:600; margin:0 0 0.3rem;
}}
.stats {{
  display:grid; grid-template-columns:repeat(auto-fit,minmax(120px,1fr));
  gap:0.6rem; margin-bottom:1.75rem;
}}
.stat {{
  background:var(--paper); border:1px solid var(--line); border-radius:10px;
  padding:0.85rem 0.75rem; display:flex; flex-direction:column; gap:0.15rem;
}}
.stat-count {{ font-family:var(--font-display); font-size:1.7rem; color:var(--pine-deep); }}
.stat-label {{ font-size:0.68rem; text-transform:uppercase; letter-spacing:.1em; color:var(--ink-soft); }}
.section {{ margin-bottom:2rem; }}
.section-head {{ margin-bottom:0.6rem; }}
.section-title {{
  font-family:var(--font-display); font-weight:400; font-size:1.25rem;
  color:var(--pine-deep); margin:0;
}}
.caveat {{
  font-size:0.78rem; color:var(--ink-soft); background:var(--paper);
  border-left:3px solid var(--brass); padding:0.5rem 0.75rem; margin:0 0 0.75rem;
  border-radius:0 6px 6px 0;
}}
details.group {{
  background:var(--paper); border:1px solid var(--line); border-radius:10px;
  margin-bottom:0.6rem; overflow:hidden;
}}
details.group summary {{
  cursor:pointer; list-style:none; padding:0.7rem 0.9rem;
  display:flex; align-items:center; justify-content:space-between;
  font-family:var(--font-display); font-size:1rem; color:var(--pine-deep);
}}
details.group summary::-webkit-details-marker {{ display:none; }}
details.group summary::after {{
  content:"+"; font-family:var(--font-body); color:var(--brass); font-size:1.1rem;
  margin-left:0.5rem;
}}
details.group[open] summary::after {{ content:"–"; }}
.group-title {{ display:flex; align-items:center; gap:0.5rem; }}
.group-count {{
  font-family:var(--font-body); font-size:0.7rem; color:var(--paper);
  background:var(--sage); border-radius:999px; padding:0.1rem 0.55rem;
  font-weight:600; margin-left:0.5rem;
}}
.group-body {{ border-top:1px solid var(--line); padding:0.3rem 0.9rem 0.5rem; }}
.group-body.no-collapse {{
  background:var(--paper); border:1px solid var(--line); border-radius:10px;
  border-top:1px solid var(--line); padding:0.3rem 0.9rem 0.5rem;
}}
.row {{ padding:0.6rem 0; border-bottom:1px solid var(--line); }}
.row:last-child {{ border-bottom:none; }}
.row-head {{ display:flex; flex-wrap:wrap; align-items:center; gap:0.4rem; }}
.row-name {{ font-weight:600; margin-right:0.1rem; }}
.row-days {{ margin-left:auto; font-size:0.75rem; color:var(--ink-soft); white-space:nowrap; }}
.row-detail {{ margin:0.3rem 0 0; font-size:0.82rem; color:var(--ink-soft); }}
.row-context {{ font-style:italic; }}
.chip {{
  font-size:0.65rem; text-transform:uppercase; letter-spacing:.08em;
  font-weight:600; padding:0.12rem 0.5rem; border-radius:999px;
  border:1px solid currentColor;
}}
.chip-ink-soft {{ color:var(--ink-soft); }}
.chip-brass {{ color:var(--brass); }}
.chip-sage {{ color:var(--sage); }}
.empty {{ color:var(--ink-soft); font-size:0.85rem; padding:0.5rem 0; margin:0; }}
.footer {{ border-top:1px solid var(--line); padding-top:1rem; margin-top:1rem; }}
.freshness {{ list-style:none; margin:0; padding:0; font-size:0.78rem; color:var(--ink-soft); }}
.freshness li {{ display:flex; justify-content:space-between; gap:0.75rem; padding:0.25rem 0; }}
.freshness li span:last-child {{ text-align:right; color:var(--ink); }}
</style>
</head>
<body>
<div class="wrap">
<header class="top">
<p class="eyebrow">Personal CRM</p>
<h1>CRM Dashboard</h1>
<p class="sub">Generated {generated}</p>
</header>
<div class="stats">
{stat_tiles}
</div>
{turn_section}
{overdue_section}
{birthday_section}
{footer}
</div>
</body>
</html>
"""


def render_page(your_turn, waiting, overdue_by_tier, birthdays) -> str:
    overdue_total = sum(len(v) for v in overdue_by_tier.values())
    stat_tiles = "".join([
        render_stat_tile("Your Turn", len(your_turn)),
        render_stat_tile("Waiting on Them", len(waiting)),
        render_stat_tile("Overdue", overdue_total),
        render_stat_tile("Birthdays (14d)", len(birthdays)),
    ])
    return PAGE_TEMPLATE.format(
        generated=datetime.now().strftime("%a %b %-d, %-I:%M %p"),
        stat_tiles=stat_tiles,
        turn_section=render_turn_section(your_turn, waiting),
        overdue_section=render_overdue_section(overdue_by_tier),
        birthday_section=render_birthday_section(birthdays),
        footer=render_footer(),
    )


def main():
    lock = JobLock("crm-dashboard") if JobLock else None
    if lock and not lock.acquire():
        print("crm-dashboard already running, skipping.")
        return

    try:
        crm = load_crm()
        tracker = load_tracker()
        tier_by_name, cadence_by_name = load_sae()

        your_turn, waiting = build_turn_sections(tracker)
        overdue_by_tier = build_overdue(crm, tier_by_name, cadence_by_name)
        birthdays = build_birthdays(crm)

        html = render_page(your_turn, waiting, overdue_by_tier, birthdays)
        OUT_HTML.parent.mkdir(parents=True, exist_ok=True)
        OUT_HTML.write_text(html)

        overdue_total = sum(len(v) for v in overdue_by_tier.values())
        summary = (
            f"{len(your_turn)} your-turn, {len(waiting)} waiting, "
            f"{overdue_total} overdue, {len(birthdays)} birthdays"
        )
        print(f"Written: {OUT_HTML}")
        print(summary)
        notify_success("crm-dashboard", summary)
    except Exception as e:
        notify_failure("crm-dashboard", str(e))
        raise
    finally:
        if lock:
            lock.release()


if __name__ == "__main__":
    main()
