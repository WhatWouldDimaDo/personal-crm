#!/usr/bin/env python3
"""Communication Intelligence Scanner

Reads iMessage (chat.db) and Call History (CallHistory.storedata) to produce
per-person last_contact dates, matched against the CRM database.

  --write-crm     Write last_contact_date, last_platform, contact_count_90d
                  back to crm_database.json AND advance SAE cadence last: dates
  iMessage_handle field in CRM is used as an alternate match handle (email or
                  phone) — useful for contacts added in-person with no phone yet.
  New contacts    Unmatched handles with 3+ msgs or 1+ calls in 90d are collected
                  and written to comm_scan_results.json under 'new_contacts'.

Usage:
    python3 scripts/comm_scan.py                 # Scan only, write JSON
    python3 scripts/comm_scan.py --dry-run       # Print results + preview CRM/SAE changes, no writes
    python3 scripts/comm_scan.py --write-crm     # Scan + write back to CRM JSON + update SAE cadence

All paths (CRM database, phone index, SAE file, iMessage DB, etc.) come from
config.py and can be overridden with environment variables — see that file.
"""
import json
import plistlib
import re
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config

OUTPUT_FILE = config.OUTPUT_DIR / "comm_scan_results.json"
LOG_DIR = config.OUTPUT_DIR

# Epoch references
APPLE_EPOCH          = datetime(2001, 1, 1)
IMESSAGE_NS_DIVISOR  = 1_000_000_000       # iMessage date: nanoseconds since Apple epoch
NINETY_DAYS_AGO      = datetime.now() - timedelta(days=90)

# Platform enum — canonical values
PLATFORM_IMESSAGE = "imessage"
PLATFORM_CALL     = "call"
PLATFORM_IN_PERSON = "in_person"

# Minimum activity thresholds for new-contact detection
NEW_CONTACT_MIN_MSGS  = 3
NEW_CONTACT_MIN_CALLS = 1

# --- Availability signal extraction ---
# NOTE: this reads the CONTENT of incoming messages (not just timestamps) to
# guess whether someone recently signaled being free, busy, or traveling.
# It only reads messages from people already in your CRM/phone index, only
# looks 30 days back, and every flag expires after 14 days. It's still worth
# thinking hard about before you turn it on — see README "A feature that
# needs your consent, not just your code" before enabling --write-crm.
AVAILABILITY_LOOKBACK_DAYS = 30
AVAILABILITY_EXPIRY_DAYS   = 14

AVAILABILITY_PATTERNS = {
    "traveling": [
        r"out of town",
        r"out of the country",
        r"traveling (?:until|through|till)",
        r"in (?:NYC|LA|Chicago|Miami|Europe|[A-Z][a-z]+ for \d+ days)",
        r"gone (?:until|through|this week)",
        r"back (?:on|monday|tuesday|wednesday|thursday|friday|[A-Z][a-z]+day)",
        r"back (?:next week|in \d+ days)",
    ],
    "available": [
        r"just got back",
        r"back in (?:town)",
        r"back home",
        r"home now",
    ],
    "heads_down": [
        r"crazy week",
        r"slammed",
        r"super busy (?:until|till|through|this)",
        r"heads down",
    ],
    "open": [
        r"free (?:this|next) (?:weekend|week|friday|saturday|sunday)",
        r"down for (?:something|anything|plans)",
        r"what are you (?:up to|doing) (?:this|next)",
        r"let'?s (?:do something|hang|get together)",
    ],
}
# Compile once; check in signal-priority order (freshest message wins overall,
# but within one message "available" beats "traveling" etc. by this order)
_AVAIL_COMPILED = [
    (status, re.compile("|".join(pats), re.IGNORECASE))
    for status, pats in AVAILABILITY_PATTERNS.items()
]


# --- Phone / handle normalization ---

def normalize_phone(raw: str) -> str | None:
    """Normalize to E.164 (+1XXXXXXXXXX for US). Returns None for non-phone input."""
    if not raw:
        return None
    if "@" in raw:
        return raw.lower().strip()          # email handle — return as-is
    cleaned = re.sub(r"[^\d+]", "", raw)
    if not cleaned:
        return None
    digits = cleaned.lstrip("+")
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    if len(digits) > 11:
        return f"+{digits}"
    return None


def normalize_platform(raw: str) -> str:
    """Normalize last_platform to canonical enum."""
    if not raw:
        return PLATFORM_IMESSAGE
    r = raw.lower().strip()
    if r in ("imessage", "sms", "text"):
        return PLATFORM_IMESSAGE
    if r in ("call", "phone", "facetime"):
        return PLATFORM_CALL
    if r in ("in_person", "in-person", "inperson", "person"):
        return PLATFORM_IN_PERSON
    return r  # pass through unknown values (linkedin, email, facebook, etc.)


def extract_message_text(text_val, attributed_body_val) -> str | None:
    """Extract text from a message row, handling attributedBody for macOS Ventura+.

    On Ventura+, many messages store content in attributedBody (NSArchiver
    streamtyped format) rather than the plain text column.

    Two formats handled:
    1. NSKeyedArchiver (bplist00) — newer format, parseable with plistlib
    2. NSArchiver streamtyped — older format, requires byte-level extraction
       via NSString marker at offset +14
    """
    if text_val:
        return text_val
    if not attributed_body_val:
        return None
    data = bytes(attributed_body_val)
    # Try NSKeyedArchiver (bplist00) first
    if data[:6] == b'bplist':
        try:
            plist = plistlib.loads(data)
            for obj in plist.get("$objects", []):
                if (
                    isinstance(obj, str)
                    and obj
                    and obj != "$null"
                    and not obj.startswith("NS")
                    and len(obj) > 3
                ):
                    return obj
        except Exception:
            pass
    # NSArchiver streamtyped — length-aware NSString marker extraction
    # iMessage uses this format for most attributedBody blobs.
    # Layout: NSString(8) + \x01\x94\x84\x01+(5) + length_byte(s) + text
    try:
        idx = data.rfind(b'NSString')
        if idx > -1:
            offset = idx + 13  # position of length indicator
            lb = data[offset]
            if lb < 0x80:
                # Single-byte length
                text = data[offset + 1:offset + 1 + lb].decode('utf-8', errors='ignore')
            elif lb == 0x81:
                # Two-byte: length in next byte; text may have an extra \x00 padding
                length = data[offset + 1]
                cand_a = data[offset + 2:offset + 2 + length].decode('utf-8', errors='ignore')
                cand_b = data[offset + 3:offset + 3 + length].decode('utf-8', errors='ignore')
                pa = ''.join(c for c in cand_a if c.isprintable() or c in ('\n', '\t'))
                pb = ''.join(c for c in cand_b if c.isprintable() or c in ('\n', '\t'))
                text = pa if len(pa) >= len(pb) else pb
            else:
                return None
            cleaned = ''.join(c for c in text if c.isprintable() or c in ('\n', '\t')).strip()
            if len(cleaned) > 3:
                return cleaned
    except Exception:
        pass
    return None


# --- Apple Contacts (AddressBook) lookup ---

def load_address_book() -> dict[str, str]:
    """Load Apple Contacts → {normalized_handle: "Full Name"}.

    Tries each known AddressBook source, uses the one with the most phone
    records. Returns a dict keyed by E.164 phone numbers and lowercase email
    addresses.
    """
    best_db = None
    best_count = 0
    for path in config.ADDRESSBOOK_SOURCES:
        if not path.exists():
            continue
        # mode=ro first so WAL-buffered recent contacts are seen (immutable=1
        # ignores the WAL — see README's "WAL vs immutable=1" callout);
        # immutable fallback only if the plain open fails.
        conn = None
        for uri in (f"file:{path}?mode=ro", f"file:{path}?mode=ro&immutable=1"):
            try:
                conn = sqlite3.connect(uri, uri=True)
                conn.execute("SELECT 1 FROM ZABCDPHONENUMBER LIMIT 1")
                break
            except Exception:
                conn = None
        if conn is None:
            continue
        try:
            count = conn.execute("SELECT COUNT(*) FROM ZABCDPHONENUMBER").fetchone()[0]
            conn.close()
            if count > best_count:
                best_count = count
                best_db = path
        except Exception:
            continue

    if not best_db or best_count == 0:
        return {}

    ab: dict[str, str] = {}
    try:
        conn = None
        for uri in (f"file:{best_db}?mode=ro", f"file:{best_db}?mode=ro&immutable=1"):
            try:
                conn = sqlite3.connect(uri, uri=True)
                conn.execute("SELECT 1 FROM ZABCDRECORD LIMIT 1")
                break
            except Exception:
                conn = None
        if conn is None:
            raise RuntimeError(f"cannot open AddressBook DB {best_db} (mode=ro or immutable=1)")
        conn.execute("PRAGMA query_only=ON")

        name_expr = "COALESCE(NULLIF(TRIM(COALESCE(r.ZFIRSTNAME,'') || ' ' || COALESCE(r.ZLASTNAME,'')), ''), r.ZNICKNAME, r.ZORGANIZATION)"

        # Phones
        for full_number, name in conn.execute(f"""
            SELECT p.ZFULLNUMBER, {name_expr}
            FROM ZABCDPHONENUMBER p
            JOIN ZABCDRECORD r ON p.ZOWNER = r.Z_PK
            WHERE p.ZFULLNUMBER IS NOT NULL
              AND (r.ZFIRSTNAME IS NOT NULL OR r.ZLASTNAME IS NOT NULL OR r.ZORGANIZATION IS NOT NULL)
        """):
            if not full_number or not name:
                continue
            norm = normalize_phone(full_number)
            if norm and "@" not in norm:
                ab[norm] = name.strip()

        # Emails
        for email, name in conn.execute(f"""
            SELECT e.ZADDRESS, {name_expr}
            FROM ZABCDEMAILADDRESS e
            JOIN ZABCDRECORD r ON e.ZOWNER = r.Z_PK
            WHERE e.ZADDRESS IS NOT NULL
              AND (r.ZFIRSTNAME IS NOT NULL OR r.ZLASTNAME IS NOT NULL OR r.ZORGANIZATION IS NOT NULL)
        """):
            if not email or not name:
                continue
            ab[email.lower().strip()] = name.strip()

        conn.close()
    except Exception as e:
        print(f"  [WARN] AddressBook lookup failed: {e}")

    return ab


# --- CRM loading ---

def load_crm() -> dict[str, dict]:
    """Load the CRM JSON. Returns {name: {phone, email, ...}}."""
    if not config.CRM_DATABASE.exists():
        print(f"  [WARN] CRM database not found: {config.CRM_DATABASE}")
        return {}
    with open(config.CRM_DATABASE) as f:
        return json.load(f)


def build_phone_index(crm: dict) -> tuple[dict[str, str], dict[str, str]]:
    """Build reverse lookup: normalized_handle -> name.

    Checks both 'phone' and 'iMessage_handle' fields. The 'iMessage_handle'
    field allows matching contacts who use iMessage via email (common for
    contacts met in-person before getting their phone number).
    """
    phone_to_name = {}
    email_to_name = {}
    for name, info in crm.items():
        # Primary phone
        phone = normalize_phone(info.get("phone", ""))
        if phone and "@" not in phone:
            phone_to_name[phone] = name
        # Email
        email = (info.get("email") or "").lower().strip()
        if email:
            email_to_name[email] = name
        # iMessage_handle override (can be phone or email for iMessage matching)
        handle = normalize_phone(info.get("iMessage_handle", ""))
        if handle:
            if "@" in handle:
                email_to_name[handle] = name
            else:
                phone_to_name[handle] = name
    return phone_to_name, email_to_name


def load_gc_phone_index(crm_phones: set, crm_emails: set) -> dict[str, str]:
    """Load the phone master (gc_phone_index.json) → {normalized_phone: "Name"}.

    Only returns entries NOT already covered by the CRM database so the CRM
    layer takes precedence — the phone master is the fallback for "everyone",
    the CRM is the curated layer for "people who matter."

    Returns {normalized_handle: display_name} for phone-master-only contacts.
    """
    if not config.GC_PHONE_INDEX.exists():
        return {}
    try:
        with open(config.GC_PHONE_INDEX) as f:
            raw = json.load(f)
    except Exception:
        return {}

    gc = {}
    for handle, info in raw.items():
        name = info.get("name", "").strip()
        if not name:
            continue
        norm = normalize_phone(handle) if "@" not in handle else handle.lower().strip()
        if not norm:
            continue
        # Skip if already covered by CRM (CRM takes precedence)
        if norm in crm_phones or norm in crm_emails:
            continue
        gc[norm] = name
    return gc


# --- iMessage scanner ---

def scan_imessage(
    phone_to_name: dict, email_to_name: dict, all_phones: set,
    gc_phone_to_name: dict | None = None,
) -> tuple[dict[str, dict], dict[str, dict], list[dict]]:
    """Scan iMessage for last message date per CRM person + phone-master contacts
    + new contact detection.

    Returns:
        matched:      {name: {last_imessage, imessage_count_90d}}   — CRM people
        gc_matched:   {name: {last_imessage, imessage_count_90d}}   — phone-master-only people
        new_contacts: [{handle, message_count_90d, first_seen}]     — truly unknown
    """
    gc_phone_to_name = gc_phone_to_name or {}
    if not config.IMESSAGE_DB.exists():
        print(f"  [WARN] iMessage DB not found: {config.IMESSAGE_DB}")
        return {}, {}, []

    # mode=ro first so WAL-buffered recent messages are seen (immutable=1
    # ignores the WAL — see README's "WAL vs immutable=1" callout, this is the
    # bug that inspired it); immutable fallback only if the plain open fails.
    conn = None
    for uri in (f"file:{config.IMESSAGE_DB}?mode=ro", f"file:{config.IMESSAGE_DB}?mode=ro&immutable=1"):
        try:
            conn = sqlite3.connect(uri, uri=True)
            conn.execute("SELECT 1 FROM message LIMIT 1")
            break
        except Exception:
            conn = None
    if conn is None:
        print("  [WARN] Cannot access iMessage DB (TCC/Full Disk Access restricted)")
        return {}, {}, []
    conn.execute("PRAGMA query_only=ON")

    ninety_days_ns = int((NINETY_DAYS_AGO - APPLE_EPOCH).total_seconds() * IMESSAGE_NS_DIVISOR)
    # For new contact detection: first_seen
    ninety_days_first_ns = ninety_days_ns

    query = """
        SELECT
            h.id AS handle_id,
            MAX(m.date) AS last_date,
            COUNT(CASE WHEN m.date >= ? THEN 1 END) AS count_90d,
            MIN(CASE WHEN m.date >= ? THEN m.date END) AS first_seen_90d
        FROM message m
        JOIN handle h ON m.handle_id = h.ROWID
        GROUP BY h.id
    """

    matched    = {}   # CRM people
    gc_matched = {}   # phone-master-only people
    new_contacts = []

    for handle_id, last_date_ns, count_90d, first_seen_ns in conn.execute(
        query, (ninety_days_ns, ninety_days_first_ns)
    ):
        if not handle_id:
            continue

        # Resolve — CRM first, then phone master
        name    = None
        gc_name = None
        if "@" in handle_id:
            h = handle_id.lower().strip()
            name = email_to_name.get(h)
            if not name:
                gc_name = gc_phone_to_name.get(h)
        else:
            norm = normalize_phone(handle_id)
            if norm:
                name = phone_to_name.get(norm)
                if not name:
                    gc_name = gc_phone_to_name.get(norm)

        last_date_str = None
        if last_date_ns and last_date_ns > 0:
            last_dt = APPLE_EPOCH + timedelta(seconds=last_date_ns / IMESSAGE_NS_DIVISOR)
            last_date_str = last_dt.strftime("%Y-%m-%d")

        def _accumulate(bucket: dict, key: str) -> None:
            if key in bucket:
                ex = bucket[key]
                if last_date_str and (not ex["last_imessage"] or last_date_str > ex["last_imessage"]):
                    ex["last_imessage"] = last_date_str
                ex["imessage_count_90d"] += count_90d
            else:
                bucket[key] = {"last_imessage": last_date_str, "imessage_count_90d": count_90d}

        if name:
            _accumulate(matched, name)
        elif gc_name:
            _accumulate(gc_matched, gc_name)
        else:
            # Truly unknown — collect for new contact detection
            if count_90d >= NEW_CONTACT_MIN_MSGS:
                first_seen_str = None
                if first_seen_ns and first_seen_ns > 0:
                    first_dt = APPLE_EPOCH + timedelta(seconds=first_seen_ns / IMESSAGE_NS_DIVISOR)
                    first_seen_str = first_dt.strftime("%Y-%m-%d")
                new_contacts.append({
                    "handle": handle_id,
                    "message_count_90d": count_90d,
                    "first_seen": first_seen_str,
                    "source": "imessage",
                })

    conn.close()
    return matched, gc_matched, new_contacts


# --- Call history scanner ---

def scan_calls(
    phone_to_name: dict, all_phones: set,
    gc_phone_to_name: dict | None = None,
) -> tuple[dict[str, dict], dict[str, dict], list[dict]]:
    """Scan call history. Returns CRM matched + phone-master matched + new contacts."""
    gc_phone_to_name = gc_phone_to_name or {}
    if not config.CALL_HISTORY_DB.exists():
        print(f"  [WARN] Call history DB not found: {config.CALL_HISTORY_DB}")
        return {}, {}, []

    conn = None
    for uri in (f"file:{config.CALL_HISTORY_DB}?mode=ro", f"file:{config.CALL_HISTORY_DB}?mode=ro&immutable=1"):
        try:
            conn = sqlite3.connect(uri, uri=True)
            conn.execute("SELECT 1 FROM ZCALLRECORD LIMIT 1")
            break
        except Exception:
            conn = None
    if conn is None:
        print("  [WARN] Cannot access Call history DB (TCC/Full Disk Access restricted)")
        return {}, {}, []
    conn.execute("PRAGMA query_only=ON")

    ninety_days_s = (NINETY_DAYS_AGO - APPLE_EPOCH).total_seconds()

    query = """
        SELECT
            ZADDRESS,
            MAX(ZDATE) AS last_date,
            COUNT(CASE WHEN ZDATE >= ? THEN 1 END) AS count_90d,
            ZORIGINATED,
            MIN(CASE WHEN ZDATE >= ? THEN ZDATE END) AS first_seen_90d
        FROM ZCALLRECORD
        WHERE ZADDRESS IS NOT NULL AND ZADDRESS != ''
        GROUP BY ZADDRESS
    """

    matched    = {}
    gc_matched = {}
    new_contacts = []

    for address, last_date_s, count_90d, originated, first_seen_s in conn.execute(
        query, (ninety_days_s, ninety_days_s)
    ):
        if not address:
            continue
        norm = normalize_phone(address)
        if not norm:
            continue

        name    = phone_to_name.get(norm)
        gc_name = gc_phone_to_name.get(norm) if not name else None

        last_date_str = None
        if last_date_s and last_date_s > 0:
            last_dt = APPLE_EPOCH + timedelta(seconds=last_date_s)
            last_date_str = last_dt.strftime("%Y-%m-%d")

        direction = "outgoing" if originated == 1 else "incoming"

        def _accumulate_call(bucket: dict, key: str) -> None:
            if key in bucket:
                ex = bucket[key]
                if last_date_str and (not ex["last_call"] or last_date_str > ex["last_call"]):
                    ex["last_call"] = last_date_str
                    ex["last_call_direction"] = direction
                ex["call_count_90d"] += count_90d
            else:
                bucket[key] = {
                    "last_call": last_date_str,
                    "call_count_90d": count_90d,
                    "last_call_direction": direction,
                }

        if name:
            _accumulate_call(matched, name)
        elif gc_name:
            _accumulate_call(gc_matched, gc_name)
        else:
            if count_90d >= NEW_CONTACT_MIN_CALLS:
                first_seen_str = None
                if first_seen_s and first_seen_s > 0:
                    first_dt = APPLE_EPOCH + timedelta(seconds=first_seen_s)
                    first_seen_str = first_dt.strftime("%Y-%m-%d")
                new_contacts.append({
                    "handle": norm,
                    "call_count_90d": count_90d,
                    "first_seen": first_seen_str,
                    "source": "call",
                })

    conn.close()
    return matched, gc_matched, new_contacts


# --- Merge ---

def merge_results(crm: dict, imessage: dict, calls: dict) -> dict:
    """Merge iMessage and call data per CRM person."""
    people = {}
    for name in crm:
        im = imessage.get(name, {})
        cl = calls.get(name, {})

        last_imessage = im.get("last_imessage")
        last_call     = cl.get("last_call")

        dates = [d for d in [last_imessage, last_call] if d]
        last_contact = max(dates) if dates else None

        contact_count_90d = im.get("imessage_count_90d", 0) + cl.get("call_count_90d", 0)

        if last_contact or contact_count_90d > 0:
            # Determine platform from whichever was more recent
            if last_imessage and last_call:
                platform = PLATFORM_IMESSAGE if last_imessage >= last_call else PLATFORM_CALL
            elif last_imessage:
                platform = PLATFORM_IMESSAGE
            elif last_call:
                platform = PLATFORM_CALL
            else:
                platform = None

            entry = {
                "last_imessage": last_imessage,
                "last_call": last_call,
                "last_contact": last_contact,
                "contact_count_90d": contact_count_90d,
                "last_platform_detected": platform,
            }
            if cl.get("last_call_direction"):
                entry["last_call_direction"] = cl["last_call_direction"]
            people[name] = entry

    return people


def merge_new_contacts(im_new: list, call_new: list) -> list:
    """Deduplicate new contacts across iMessage and calls."""
    seen = {}
    for nc in im_new + call_new:
        h = nc["handle"]
        if h not in seen:
            seen[h] = nc
        else:
            # Merge counts
            seen[h]["message_count_90d"] = seen[h].get("message_count_90d", 0) + nc.get("message_count_90d", 0)
            seen[h]["call_count_90d"]    = seen[h].get("call_count_90d", 0) + nc.get("call_count_90d", 0)
            # Keep earliest first_seen
            if nc.get("first_seen") and (not seen[h].get("first_seen") or nc["first_seen"] < seen[h]["first_seen"]):
                seen[h]["first_seen"] = nc["first_seen"]
    return sorted(seen.values(), key=lambda x: -(x.get("message_count_90d", 0) + x.get("call_count_90d", 0)))


def merge_gc_results(im_gc: dict, call_gc: dict) -> dict:
    """Merge phone-master-only iMessage + call data into a contact activity dict.

    Same shape as merge_results() output but for people in the phone master
    who are not in the CRM database. No CRM writeback for these.
    Returns {name: {last_contact, contact_count_90d, last_imessage, last_call, last_platform_detected}}
    """
    all_names = set(im_gc) | set(call_gc)
    result = {}
    for name in all_names:
        im = im_gc.get(name, {})
        cl = call_gc.get(name, {})
        last_imessage = im.get("last_imessage")
        last_call     = cl.get("last_call")
        dates = [d for d in [last_imessage, last_call] if d]
        last_contact = max(dates) if dates else None
        count = im.get("imessage_count_90d", 0) + cl.get("call_count_90d", 0)
        if last_imessage and last_call:
            platform = PLATFORM_IMESSAGE if last_imessage >= last_call else PLATFORM_CALL
        elif last_imessage:
            platform = PLATFORM_IMESSAGE
        elif last_call:
            platform = PLATFORM_CALL
        else:
            platform = None
        entry = {
            "last_imessage": last_imessage,
            "last_call": last_call,
            "last_contact": last_contact,
            "contact_count_90d": count,
            "last_platform_detected": platform,
        }
        if cl.get("last_call_direction"):
            entry["last_call_direction"] = cl["last_call_direction"]
        result[name] = entry
    return result


# --- Awaiting-reply scanner ---

AWAITING_REPLY_MIN_DAYS = 3
AWAITING_REPLY_MAX_DAYS = 60   # older than this is dormancy (overdue tracking's job), not a nudge


def scan_awaiting_reply(crm: dict) -> dict[str, dict]:
    """Find CRM people whose DM thread ends with an unanswered message FROM you.

    'Overdue' means it's time to reach out; 'awaiting reply' means you already
    did and they went quiet — a different action (nudge or let go, not re-open).
    Returns {name: {since, days}} for threads stale >= AWAITING_REPLY_MIN_DAYS days.
    """
    if not config.IMESSAGE_DB.exists():
        return {}
    try:
        conn = sqlite3.connect(f"file:{config.IMESSAGE_DB}?mode=ro", uri=True)
        conn.execute("SELECT 1 FROM message LIMIT 1")
    except Exception:
        try:
            conn = sqlite3.connect(f"file:{config.IMESSAGE_DB}?mode=ro&immutable=1", uri=True)
        except Exception:
            return {}
    conn.execute("PRAGMA query_only=ON")

    handle_rows: dict[str, list[int]] = {}
    for rowid, hid in conn.execute("SELECT ROWID, id FROM handle"):
        norm = normalize_phone(hid) if "@" not in hid else hid.lower().strip()
        if norm:
            handle_rows.setdefault(norm, []).append(rowid)

    chat_sizes = dict(conn.execute(
        "SELECT chat_id, COUNT(*) FROM chat_handle_join GROUP BY chat_id"))

    out: dict[str, dict] = {}
    now = datetime.now()
    for name, info in crm.items():
        handles = [normalize_phone(info.get("phone", "")),
                   normalize_phone(info.get("iMessage_handle", "")),
                   (info.get("email") or "").lower().strip() or None]
        rowids = [r for h in handles if h for r in handle_rows.get(h, [])]
        if not rowids:
            continue
        ph = ",".join("?" * len(rowids))
        dm_chats = [cid for (cid,) in conn.execute(
            f"SELECT DISTINCT chat_id FROM chat_handle_join WHERE handle_id IN ({ph})",
            rowids) if chat_sizes.get(cid, 0) <= 2]
        if not dm_chats:
            continue
        cph = ",".join("?" * len(dm_chats))
        row = conn.execute(
            f"SELECT m.is_from_me, MAX(m.date) FROM message m "
            f"JOIN chat_message_join j ON j.message_id = m.ROWID "
            f"WHERE j.chat_id IN ({cph})", dm_chats).fetchone()
        if not row or row[1] is None:
            continue
        # MAX(date) row's is_from_me isn't guaranteed by SQLite semantics — re-fetch properly
        last = conn.execute(
            f"SELECT m.is_from_me, m.date FROM message m "
            f"JOIN chat_message_join j ON j.message_id = m.ROWID "
            f"WHERE j.chat_id IN ({cph}) ORDER BY m.date DESC LIMIT 1", dm_chats).fetchone()
        if not last or last[0] != 1:
            continue  # last word was theirs (or empty) — not awaiting
        sent_at = APPLE_EPOCH + timedelta(seconds=last[1] / IMESSAGE_NS_DIVISOR)
        days = (now - sent_at).days
        if AWAITING_REPLY_MIN_DAYS <= days <= AWAITING_REPLY_MAX_DAYS:
            out[name] = {"since": sent_at.strftime("%Y-%m-%d"), "days": days}

    conn.close()
    return out


def scan_thread_turn(names: list[str], crm: dict) -> dict[str, dict]:
    """Bidirectional version of scan_awaiting_reply(), scoped to specific names.

    Unlike scan_awaiting_reply() (which only flags "I sent last, no reply"),
    this records the last message's direction either way — the input to the
    outreach tracker's "whose turn" field. Only scans the given names (typically
    the people currently tracked in outreach_tracker.json), not the full CRM.

    Returns {name: {last_sender: "me"|"them", last_message_date: "YYYY-MM-DD"}}.
    """
    if not config.IMESSAGE_DB.exists():
        return {}
    try:
        conn = sqlite3.connect(f"file:{config.IMESSAGE_DB}?mode=ro", uri=True)
        conn.execute("SELECT 1 FROM message LIMIT 1")
    except Exception:
        try:
            conn = sqlite3.connect(f"file:{config.IMESSAGE_DB}?mode=ro&immutable=1", uri=True)
        except Exception:
            return {}
    conn.execute("PRAGMA query_only=ON")

    handle_rows: dict[str, list[int]] = {}
    for rowid, hid in conn.execute("SELECT ROWID, id FROM handle"):
        norm = normalize_phone(hid) if "@" not in hid else hid.lower().strip()
        if norm:
            handle_rows.setdefault(norm, []).append(rowid)

    chat_sizes = dict(conn.execute(
        "SELECT chat_id, COUNT(*) FROM chat_handle_join GROUP BY chat_id"))

    out: dict[str, dict] = {}
    for name in names:
        info = crm.get(name, {})
        handles = [normalize_phone(info.get("phone", "")),
                   normalize_phone(info.get("iMessage_handle", "")),
                   (info.get("email") or "").lower().strip() or None]
        rowids = [r for h in handles if h for r in handle_rows.get(h, [])]
        if not rowids:
            continue
        ph = ",".join("?" * len(rowids))
        dm_chats = [cid for (cid,) in conn.execute(
            f"SELECT DISTINCT chat_id FROM chat_handle_join WHERE handle_id IN ({ph})",
            rowids) if chat_sizes.get(cid, 0) <= 2]
        if not dm_chats:
            continue
        cph = ",".join("?" * len(dm_chats))
        last = conn.execute(
            f"SELECT m.is_from_me, m.date FROM message m "
            f"JOIN chat_message_join j ON j.message_id = m.ROWID "
            f"WHERE j.chat_id IN ({cph}) ORDER BY m.date DESC LIMIT 1", dm_chats).fetchone()
        if not last or last[1] is None:
            continue
        sent_at = APPLE_EPOCH + timedelta(seconds=last[1] / IMESSAGE_NS_DIVISOR)
        out[name] = {
            "last_sender": "me" if last[0] == 1 else "them",
            "last_message_date": sent_at.strftime("%Y-%m-%d"),
        }

    conn.close()
    return out


# --- Availability scanner ---

def scan_availability(phone_to_name: dict, email_to_name: dict) -> dict[str, dict]:
    """Scan incoming iMessage content (last 30 days) for availability signals.

    Only is_from_me = 0 (what the contact said). Freshest signal per person
    wins. Returns {name: {status, note, detected, expires}}.

    Uses plain mode=ro (NOT immutable=1) so WAL-buffered recent messages are
    included when the -shm file is readable; falls back to immutable if the
    plain open fails.
    """
    if not config.IMESSAGE_DB.exists():
        return {}
    conn = None
    for uri in (f"file:{config.IMESSAGE_DB}?mode=ro", f"file:{config.IMESSAGE_DB}?mode=ro&immutable=1"):
        try:
            conn = sqlite3.connect(uri, uri=True)
            conn.execute("SELECT 1 FROM message LIMIT 1")
            break
        except Exception:
            conn = None
    if conn is None:
        print("  [WARN] Cannot read chat.db for availability scan")
        return {}
    conn.execute("PRAGMA query_only=ON")

    lookback = datetime.now() - timedelta(days=AVAILABILITY_LOOKBACK_DAYS)
    lookback_ns = int((lookback - APPLE_EPOCH).total_seconds() * IMESSAGE_NS_DIVISOR)

    query = """
        SELECT h.id, m.date, m.text, m.attributedBody
        FROM message m
        JOIN handle h ON m.handle_id = h.ROWID
        WHERE m.is_from_me = 0 AND m.date >= ?
        ORDER BY m.date ASC
    """

    flags: dict[str, dict] = {}
    for handle_id, date_ns, text_val, attr_body in conn.execute(query, (lookback_ns,)):
        if not handle_id:
            continue
        if "@" in handle_id:
            name = email_to_name.get(handle_id.lower().strip())
        else:
            norm = normalize_phone(handle_id)
            name = phone_to_name.get(norm) if norm else None
        if not name:
            continue
        text = extract_message_text(text_val, attr_body)
        if not text or len(text) > 2000:
            continue
        for status, rx in _AVAIL_COMPILED:
            m = rx.search(text)
            if m:
                detected = (APPLE_EPOCH + timedelta(seconds=date_ns / IMESSAGE_NS_DIVISOR))
                snippet = text.strip().replace("\n", " ")
                if len(snippet) > 120:
                    lo = max(0, m.start() - 40)
                    snippet = ("…" if lo else "") + snippet[lo:lo + 120] + "…"
                # ORDER BY date ASC → later messages simply overwrite
                flags[name] = {
                    "status": status,
                    "note": snippet,
                    "detected": detected.strftime("%Y-%m-%d"),
                    "expires": (detected + timedelta(days=AVAILABILITY_EXPIRY_DAYS)).strftime("%Y-%m-%d"),
                }
                break

    conn.close()
    # Drop signals that are already past their 14-day expiry
    today = datetime.now().strftime("%Y-%m-%d")
    return {n: f for n, f in flags.items() if f["expires"] >= today}


def write_availability(avail: dict[str, dict], dry_run: bool = False) -> tuple[int, int]:
    """Write availability flags to the CRM database; expire stale ones.

    Returns (set_count, expired_count).
    """
    if not config.CRM_DATABASE.exists():
        return 0, 0
    with open(config.CRM_DATABASE) as f:
        db = json.load(f)
    today = datetime.now().strftime("%Y-%m-%d")

    set_n = expired_n = 0
    for name, entry in db.items():
        if name in avail:
            if entry.get("availability") != avail[name]:
                set_n += 1
                if not dry_run:
                    entry["availability"] = avail[name]
        else:
            existing = entry.get("availability")
            if existing and existing.get("expires", "") < today:
                expired_n += 1
                if not dry_run:
                    entry["availability"] = None

    if not dry_run and (set_n or expired_n):
        config.assert_writable_outside_repo(config.CRM_DATABASE, "CRM_DATABASE")
        with open(config.CRM_DATABASE, "w") as f:
            json.dump(db, f, indent=2, ensure_ascii=False)
    return set_n, expired_n


def load_outreach_tracker() -> dict:
    if not config.OUTREACH_TRACKER.exists():
        return {"threads": []}
    with open(config.OUTREACH_TRACKER) as f:
        return json.load(f)


def write_outreach_turns(turns: dict[str, dict], dry_run: bool = False) -> int:
    """Write last_sender/last_message_date back to outreach_tracker.json threads.

    Follows the write_availability() pattern. Auto-advances stage
    identified -> invited when a new outbound ("me") message is detected for
    a thread still at "identified". Returns count of threads updated.
    """
    if not config.OUTREACH_TRACKER.exists():
        return 0
    tracker = load_outreach_tracker()
    today = datetime.now().strftime("%Y-%m-%d")

    updated = 0
    for thread in tracker.get("threads", []):
        turn = turns.get(thread["name"])
        if not turn:
            continue
        changed = (thread.get("last_sender") != turn["last_sender"]
                   or thread.get("last_message_date") != turn["last_message_date"])
        if not changed:
            continue
        updated += 1
        if dry_run:
            continue
        thread["last_sender"] = turn["last_sender"]
        thread["last_message_date"] = turn["last_message_date"]
        thread["updated"] = today
        if thread.get("stage") == "identified" and turn["last_sender"] == "me":
            thread["stage"] = "invited"

    if not dry_run and updated:
        config.assert_writable_outside_repo(config.OUTREACH_TRACKER, "OUTREACH_TRACKER")
        with open(config.OUTREACH_TRACKER, "w") as f:
            json.dump(tracker, f, indent=2, ensure_ascii=False)
    return updated


# --- CRM writeback ---

def write_crm_updates(people: dict, crm: dict, dry_run: bool = False) -> list[dict]:
    """Write last_contact_date, last_platform, contact_count_90d back to CRM JSON.

    Only updates if the new last_contact is more recent than what's stored.
    Never overwrites a more-recent date or an 'in_person' platform with a digital one.
    Returns list of change records for logging.
    """
    changes = []
    today = datetime.now().strftime("%Y-%m-%d")

    if not config.CRM_DATABASE.exists():
        return changes

    with open(config.CRM_DATABASE) as f:
        db = json.load(f)

    skipped = []
    for name, scan_data in people.items():
        if name not in db:
            skipped.append(name)
            continue

        entry = db[name]
        change = {"name": name, "fields": {}}

        # last_contact_date — only advance, never backdate
        scan_date = scan_data.get("last_contact")
        existing_date = entry.get("last_contact_date", "")
        if scan_date and (not existing_date or scan_date > existing_date):
            change["fields"]["last_contact_date"] = {
                "from": existing_date, "to": scan_date
            }
            if not dry_run:
                entry["last_contact_date"] = scan_date

        # last_platform — don't overwrite in_person with digital
        scan_platform = scan_data.get("last_platform_detected")
        existing_platform_raw = entry.get("last_platform")  # raw value, may be None
        existing_platform = normalize_platform(existing_platform_raw or "")
        if scan_platform and existing_platform_raw != PLATFORM_IN_PERSON:
            # Update if unset OR if detected platform differs from stored value
            if not existing_platform_raw or scan_platform != existing_platform:
                change["fields"]["last_platform"] = {
                    "from": entry.get("last_platform"), "to": scan_platform
                }
                if not dry_run:
                    entry["last_platform"] = scan_platform

        # contact_count_90d — always overwrite (it's a rolling window)
        new_count = scan_data.get("contact_count_90d", 0)
        existing_count = entry.get("contact_count_90d")
        if new_count != existing_count:
            change["fields"]["contact_count_90d"] = {
                "from": existing_count, "to": new_count
            }
            if not dry_run:
                entry["contact_count_90d"] = new_count

        # last_comm_scan — always set to today
        if not dry_run:
            entry["last_comm_scan"] = today

        if change["fields"]:
            changes.append(change)

    if skipped:
        print(f"  [WARN] {len(skipped)} scanned people missing from CRM DB: "
              f"{', '.join(skipped[:5])}{'...' if len(skipped) > 5 else ''}")

    if not dry_run:
        config.assert_writable_outside_repo(config.CRM_DATABASE, "CRM_DATABASE")
        with open(config.CRM_DATABASE, "w") as f:
            json.dump(db, f, indent=2, ensure_ascii=False)

    return changes


# --- SAE cadence update ---

def update_sae_cadence(people: dict, gc_known: dict, dry_run: bool = False) -> int:
    """Advance 'last:' dates in the SAE Cadence Targets section from scan results.

    Matches each entry in the SAE cadence section by name, then advances its
    'last:' date if the scan found more recent activity. Never backdates.
    Updates both CRM people and phone-master-known people (SAE may contain either).

    Returns the number of entries updated.
    """
    if not config.SAE_FILE.exists():
        print(f"  [WARN] SAE file not found: {config.SAE_FILE}")
        return 0

    content = config.SAE_FILE.read_text(encoding="utf-8")

    # Build {name: last_contact} from both CRM and phone-master scan buckets
    scan_dates: dict[str, str] = {}
    for name, data in {**people, **gc_known}.items():
        last = data.get("last_contact")
        if last:
            scan_dates[name] = last

    updated = 0
    new_content = content

    for name, scan_date in scan_dates.items():
        # Match: "- Name (last: YYYY-MM-DD)" or "- Name (last: unknown)"
        pattern = rf"(- {re.escape(name)} \(last: )([^\)]+)(\))"
        match = re.search(pattern, new_content)
        if not match:
            continue

        current_str = match.group(2)

        if current_str == "unknown":
            should_update = True
        else:
            try:
                should_update = scan_date > current_str   # ISO date string comparison works
            except Exception:
                should_update = False

        if should_update:
            new_content = re.sub(pattern, rf"\g<1>{scan_date}\3", new_content, count=1)
            updated += 1
            print(f"    {name}: {current_str} → {scan_date}")

    if updated > 0 and not dry_run:
        config.assert_writable_outside_repo(config.SAE_FILE, "SAE_FILE")
        config.SAE_FILE.write_text(new_content, encoding="utf-8")

    return updated


# --- Logging ---

def write_log(changes: list, people: dict, new_contacts: list, dry_run: bool):
    """Write a dated log file summarizing what changed."""
    today = datetime.now().strftime("%Y-%m-%d")
    log_path = LOG_DIR / f"comm_scan_log_{today}.json"
    log = {
        "date": today,
        "dry_run": dry_run,
        "crm_updates": changes,
        "new_contacts_detected": new_contacts,
        "total_matched": len(people),
        "total_changes": len(changes),
    }
    if not dry_run:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(log_path, "w") as f:
            json.dump(log, f, indent=2)
        print(f"  Log written: {log_path}")
    else:
        print(f"  [DRY RUN] Would write log to {log_path}")
    return log


# --- Main ---

def main():
    dry_run   = "--dry-run"   in sys.argv
    write_crm = "--write-crm" in sys.argv

    print("=== Communication Intelligence Scanner ===\n")

    # 1. Load CRM
    print("[1/5] Loading CRM database...")
    crm = load_crm()
    phone_to_name, email_to_name = build_phone_index(crm)
    all_phones = set(phone_to_name.keys())
    print(f"  {len(crm)} people  |  {len(phone_to_name)} phones  |  {len(email_to_name)} emails")

    # Identify manual-only contacts (no phone, no email, no iMessage_handle)
    manual_only = [
        name for name, info in crm.items()
        if not info.get("phone") and not info.get("email") and not info.get("iMessage_handle")
    ]
    if manual_only:
        print(f"  Manual-only (no trackable handle): {', '.join(manual_only[:8])}{'...' if len(manual_only) > 8 else ''}")

    # Load the phone master (fallback layer below the curated CRM)
    gc_phone_to_name = load_gc_phone_index(set(phone_to_name.keys()), set(email_to_name.keys()))
    print(f"  Phone master: {len(gc_phone_to_name)} additional contacts loaded")

    # 2. Scan iMessage
    print("[2/5] Scanning iMessage...")
    imessage, im_gc, im_new = scan_imessage(phone_to_name, email_to_name, all_phones, gc_phone_to_name)
    print(f"  {len(imessage)} CRM  |  {len(im_gc)} phone-master contacts  |  {len(im_new)} unknown candidates")

    # 3. Scan calls
    print("[3/5] Scanning call history...")
    calls, call_gc, call_new = scan_calls(phone_to_name, all_phones, gc_phone_to_name)
    print(f"  {len(calls)} CRM  |  {len(call_gc)} phone-master contacts  |  {len(call_new)} unknown candidates")

    # 4. Merge
    print("[4/5] Merging results...")
    people       = merge_results(crm, imessage, calls)
    gc_known     = merge_gc_results(im_gc, call_gc)
    new_contacts = merge_new_contacts(im_new, call_new)

    # Enrich truly-unknown new_contacts with AddressBook names as last resort
    print("  Loading AddressBook for unknown handle enrichment...")
    address_book = load_address_book()
    print(f"  {len(address_book)} AddressBook entries")
    for nc in new_contacts:
        handle = nc["handle"]
        if "@" in handle:
            nc["name"] = address_book.get(handle.lower())
        else:
            norm = normalize_phone(handle)
            nc["name"] = address_book.get(norm) if norm else None

    # Awaiting-reply detection (threads where you texted last, no response 3+ days)
    awaiting = scan_awaiting_reply(crm)
    if awaiting:
        print(f"  Awaiting reply ({len(awaiting)}): " +
              ", ".join(f"{n} ({d['days']}d)" for n, d in
                        sorted(awaiting.items(), key=lambda x: -x[1]['days'])[:5]))

    output = {
        "generated": datetime.now().isoformat(timespec="seconds"),
        "source": "iMessage + CallHistory",
        "awaiting_reply": dict(sorted(awaiting.items(), key=lambda x: -x[1]["days"])),
        "crm_total": len(crm),
        "matched": len(people),
        "gc_known_count": len(gc_known),
        "manual_only_contacts": manual_only,
        "people": dict(sorted(people.items(), key=lambda x: x[1].get("last_contact") or "", reverse=True)),
        "gc_known": dict(sorted(gc_known.items(), key=lambda x: x[1].get("last_contact") or "", reverse=True)),
        "new_contacts": new_contacts[:20],  # cap at 20 — truly unknown handles
    }

    if dry_run:
        print(json.dumps(output, indent=2))
    else:
        config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        with open(OUTPUT_FILE, "w") as f:
            json.dump(output, f, indent=2)
        print(f"  Written: {OUTPUT_FILE}")

    # 5. CRM writeback + SAE cadence update (if requested)
    changes = []
    if write_crm or dry_run:
        label = "[DRY RUN] " if dry_run else ""
        print(f"[5/5] {label}Writing back to CRM database...")
        changes = write_crm_updates(people, crm, dry_run=dry_run)
        print(f"  {len(changes)} records would be updated" if dry_run else f"  {len(changes)} records updated")
        for c in changes[:10]:
            fields_str = ", ".join(f"{k}: {v['from']} → {v['to']}" for k, v in c["fields"].items())
            print(f"    {c['name']}: {fields_str}")
        if len(changes) > 10:
            print(f"    ... and {len(changes)-10} more")

        print(f"  {label}Updating SAE cadence dates...")
        sae_updates = update_sae_cadence(people, gc_known, dry_run=dry_run)
        verb = "would advance" if dry_run else "advanced"
        print(f"  {sae_updates} SAE cadence dates {verb}")

    # Availability signal extraction reads message CONTENT (not just
    # timestamps), so it's gated on --write-crm alone — never on --dry-run
    # by itself. See README "A feature that needs your consent, not just
    # your code". (--write-crm --dry-run together still runs it, since
    # --write-crm is the consent; it just previews instead of writing.)
    if write_crm:
        label = "[DRY RUN] " if dry_run else ""
        print(f"  {label}Scanning availability signals...")
        avail = scan_availability(phone_to_name, email_to_name)
        set_n, expired_n = write_availability(avail, dry_run=dry_run)
        print(f"  {len(avail)} active signals  |  {set_n} written, {expired_n} expired" if not dry_run
              else f"  {len(avail)} active signals  |  {set_n} would be written, {expired_n} would expire")
        for n, f in list(avail.items())[:6]:
            print(f"    {n}: {f['status']} ({f['detected']})")

    if write_crm or dry_run:
        write_log(changes, people, new_contacts, dry_run=dry_run)
    else:
        print("[5/5] Skipped CRM writeback (use --write-crm to apply)")

    # Summary
    print(f"\n=== Summary ===")
    print(f"  CRM ({len(crm)}): {len(people)} matched  |  Phone-master-known: {len(gc_known)}  |  Unknown: {len(new_contacts)}  |  Manual-only: {len(manual_only)}")
    if write_crm and not dry_run:
        print(f"  CRM records updated: {len(changes)}")

    sorted_people = sorted(people.items(), key=lambda x: x[1].get("last_contact") or "", reverse=True)
    print(f"\n  Most recent CRM contacts:")
    for name, info in sorted_people[:5]:
        print(f"    {name}: {info['last_contact']}  (90d: {info['contact_count_90d']})")

    if gc_known:
        sorted_gc = sorted(gc_known.items(), key=lambda x: x[1].get("last_contact") or "", reverse=True)
        print(f"\n  Most recent phone-master contacts (not in CRM):")
        for name, info in sorted_gc[:5]:
            print(f"    {name}: {info['last_contact']}  (90d: {info['contact_count_90d']})")

    if new_contacts:
        print(f"\n  Truly unknown handles (not in any contact list):")
        for nc in new_contacts[:5]:
            msgs = nc.get("message_count_90d", 0)
            calls_c = nc.get("call_count_90d", 0)
            label = f" ({nc['name']})" if nc.get("name") else ""
            print(f"    {nc['handle']}{label}  msgs:{msgs}  calls:{calls_c}  first:{nc.get('first_seen')}")


if __name__ == "__main__":
    main()
