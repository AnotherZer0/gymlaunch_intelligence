"""
HubSpot sync Lambda
Reads rows from asana_agency_board_task where content_hash differs from
last_synced_hash, pushes updates to HubSpot via the batch companies API,
then records the result back to the DB.

Runs at :10 past each hour, 10 minutes after the Asana sync.

HubSpot's batch update is ATOMIC on validation errors — one bad record
(unknown dropdown option, deleted company id, stray whitespace) 400s the
entire batch. This jammed the pipeline for a month in June 2026 ("Onboard -
Stalled" wasn't a HubSpot option yet). So when a batch fails we fall back to
per-record PATCHes, and we store HubSpot's actual error message in
last_hubspot_run_status so failures are diagnosable from the DB (the deploy
user cannot read CloudWatch logs).

Auto-remediation (July 2026):
- Pasted-URL company ids ("https://app.hubspot.com/contacts/.../record/0-2/123")
  are normalized to the trailing numeric id before sending.
- 404 OBJECT_NOT_FOUND failures are PARKED: the error is recorded but
  last_synced_hash is set anyway, so the row stops retrying hourly. The fix
  for a dead id lives in Asana, and editing the task changes content_hash,
  which un-parks the row automatically. 400 validation errors keep retrying
  every hour because their fix is usually HubSpot-side (e.g. adding a missing
  dropdown option), which never changes the hash.
- Multiple Asana tasks can carry the same company id; inputs are deduped per
  company (last row wins) but EVERY task row gets its status written back,
  so duplicates can't get stuck pending forever.

Alerting: when a run ends with ACTIONABLE failures — a value HubSpot rejects
(new Asana status missing from the dropdown) or an AM missing from
account_manager_hubspot_map — an SES email goes to ALERT_TO_ADDRESS. Parked
404s are excluded (dead ids are procedural fixes in Asana). A fingerprint of
the problem set lives in hubspot_sync_alert_state (migration 018) so the email
fires only when the set CHANGES, not on every hourly retry. An alert-send
failure never breaks the sync, and the fingerprint is only advanced after a
successful send, so a failed email retries next run.

DEBUG mode (env var, default "0"): runs the real queries and mapping but
performs no HubSpot writes and no DB write-backs; returns what it WOULD
send in the invocation response.
"""

import hashlib
import json
import os
import re
import ssl
import time
from datetime import datetime, timezone, date
from email.mime.text import MIMEText

import boto3
import pg8000
import requests


# --- Config ---

# Seconds to wait for each HubSpot API call before raising a timeout error.
# Increase if you see timeout errors; decrease for faster failure detection.
REQUEST_TIMEOUT_SECONDS = 30

HUBSPOT_BATCH_SIZE = 100  # HubSpot max per batch update call
HUBSPOT_BASE_URL   = "https://api.hubapi.com"


def debug_mode() -> bool:
    return os.environ.get("DEBUG", "0") == "1"


# --- DB connection ---

def get_db_connection():
    ctx = ssl.create_default_context()
    return pg8000.connect(
        host=os.environ["DB_HOST"],
        port=5432,
        database=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        ssl_context=ctx,
    )


# --- HubSpot API helpers ---

def hubspot_headers():
    return {
        "Authorization": f"Bearer {os.environ['HUBSPOT_TOKEN']}",
        "Content-Type": "application/json",
    }


def hubspot_patch(path: str, payload: dict) -> requests.Response:
    """PATCH a single company record."""
    url = f"{HUBSPOT_BASE_URL}{path}"
    resp = requests.patch(url, json=payload, headers=hubspot_headers(), timeout=REQUEST_TIMEOUT_SECONDS)
    _handle_rate_limit(resp)
    return resp


def hubspot_batch_update(inputs: list[dict]) -> requests.Response:
    """POST to the batch companies update endpoint."""
    url = f"{HUBSPOT_BASE_URL}/crm/v3/objects/companies/batch/update"
    resp = requests.post(
        url,
        json={"inputs": inputs},
        headers=hubspot_headers(),
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    _handle_rate_limit(resp)
    return resp


def _handle_rate_limit(resp: requests.Response) -> None:
    """If HubSpot says slow down, wait exactly as long as it asks."""
    if resp.status_code == 429:
        wait = int(resp.headers.get("Retry-After", 10))
        print(f"  Rate limited by HubSpot, waiting {wait}s")
        time.sleep(wait)


def extract_hubspot_error(resp: requests.Response) -> str:
    """Pull the human-readable validation message out of a HubSpot error body."""
    try:
        data = resp.json()
        messages = [e.get("message", "") for e in data.get("errors", [])]
        detail = "; ".join(m for m in messages if m) or data.get("message", "")
    except Exception:
        detail = resp.text[:300]
    return f"HTTP {resp.status_code}: {detail}"


def normalize_company_id(raw: str) -> str:
    """
    Clean up a hubspot_company_id value coming from Asana.

    People sometimes paste the full HubSpot record URL instead of the numeric
    id (e.g. "https://app.hubspot.com/contacts/43776308/record/0-2/55405439771").
    Pull out the trailing numeric id in that case. Anything we can't recognise
    is returned stripped and will surface as a 404 (and get parked).
    """
    value = raw.strip()
    if value.isdigit():
        return value
    m = re.search(r"/record/0-2/(\d+)", value)
    if m:
        return m.group(1)
    return value


# --- Field mapping ---

def to_epoch_ms(d: date | None) -> int | None:
    """Convert a date to epoch milliseconds at UTC midnight (HubSpot date format)."""
    if d is None:
        return None
    dt = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def build_properties(row: dict, am_hubspot_id: str | None) -> dict:
    """
    Map DB row fields to HubSpot property names.

    To add a new field:
      1. Add the DB column to the SELECT in fetch_pending_rows()
      2. Add the mapping here (DB value -> HubSpot internal property name)
      3. See how_to_update.txt for full instructions including DB migration steps.

    NOTE: if the field is a HubSpot dropdown, every value coming from Asana
    must exist as an option on the HubSpot property — an unknown option is a
    validation error. The per-record fallback keeps that from blocking other
    rows, and the DB status will name the offending value.
    """
    props = {
        "agency_board_asana_task":          row["task_gid"],
        "asana_agency_status":              row["agency_status"],
        "agency_media_buyer":               row["media_buyer"],
        "client_facebook_page_name":        row["facebook_page_name"],
        "facebook_page_id":                 row["facebook_page_id"],
        "facebook_ad_acct_id":              row["facebook_ad_account_id"],
        "facebook_ad_account_name":         row["facebook_ad_account_name"],
        "high_level_subaccount_location_id": row["hl_sub_account_location_id"],
        "ad_spend_budget_daily":            row["ad_spend_budget_daily"],
        "ads_live_date":                    to_epoch_ms(row["ads_live_date"]),
        "account_manager":                  am_hubspot_id,
    }
    # Trim stray whitespace from Asana-sourced values — HubSpot rejects dropdown
    # values with leading/trailing whitespace (LEADING_TRAILING_WHITESPACE),
    # e.g. the "Downed Account - META Ad Account Disabled " Asana option.
    return {k: (v.strip() if isinstance(v, str) else v) for k, v in props.items()}


# --- DB queries ---

def fetch_pending_rows(cur) -> list[dict]:
    """Fetch all rows that have changed since the last HubSpot push."""
    cur.execute(
        """
        SELECT
            task_gid,
            hubspot_company_id,
            agency_status,
            account_manager,
            media_buyer,
            facebook_page_name,
            facebook_page_id,
            facebook_ad_account_id,
            facebook_ad_account_name,
            hl_sub_account_location_id,
            ad_spend_budget_daily,
            ads_live_date,
            coach,
            content_hash
        FROM asana_agency_board_task
        WHERE hubspot_company_id IS NOT NULL
          AND (last_synced_hash IS NULL OR content_hash != last_synced_hash)
        """
    )
    cols = [desc[0] for desc in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def fetch_am_map(cur) -> dict:
    """Return {name: hubspot_id} for all account managers."""
    cur.execute("SELECT name, hubspot_id FROM account_manager_hubspot_map")
    return {row[0]: row[1] for row in cur.fetchall()}


def mark_success(cur, task_gid: str, content_hash: str) -> None:
    cur.execute(
        """
        UPDATE asana_agency_board_task
        SET last_synced_hash     = %s,
            last_hubspot_run_status = 'success',
            last_synced_at       = now()
        WHERE task_gid = %s
        """,
        (content_hash, task_gid),
    )


def mark_error(cur, task_gid: str, error: str) -> None:
    cur.execute(
        """
        UPDATE asana_agency_board_task
        SET last_hubspot_run_status = %s
        WHERE task_gid = %s
        """,
        (f"ERROR: {error}"[:500], task_gid),
    )


def mark_parked(cur, task_gid: str, content_hash: str, error: str) -> None:
    """
    Record a permanent failure (dead company id) but set last_synced_hash so
    the row stops retrying hourly. Editing the task in Asana changes
    content_hash, which automatically un-parks it.
    """
    cur.execute(
        """
        UPDATE asana_agency_board_task
        SET last_synced_hash        = %s,
            last_hubspot_run_status = %s
        WHERE task_gid = %s
        """,
        (content_hash, f"ERROR (parked until Asana edit): {error}"[:500], task_gid),
    )


# --- Alerting ---

def build_problem_list(errors_by_id: dict, unmapped_ams: set) -> list[str]:
    """
    Distinct actionable problems from a run. Parked 404s (dead company ids)
    are deliberately excluded — fixing those is procedural, in Asana.
    """
    counts = {}
    for err in errors_by_id.values():
        if err["park"]:
            continue
        counts[err["error"][:300]] = counts.get(err["error"][:300], 0) + 1
    problems = [
        f"{msg}  [{n} compan{'y' if n == 1 else 'ies'} affected]"
        for msg, n in sorted(counts.items())
    ]
    problems += [
        f"Account manager '{name}' has no row in account_manager_hubspot_map — "
        f"their companies sync WITHOUT an owner (HubSpot keeps the stale one)"
        for name in sorted(unmapped_ams)
    ]
    return problems


def fetch_alert_fingerprint(cur) -> str:
    cur.execute("SELECT last_fingerprint FROM hubspot_sync_alert_state WHERE id = 1")
    row = cur.fetchone()
    return row[0] if row else ""


def store_alert_state(cur, fingerprint: str, problems: list[str], alerted: bool) -> None:
    cur.execute(
        """
        UPDATE hubspot_sync_alert_state
        SET last_fingerprint = %s,
            last_problems    = %s,
            last_alerted_at  = CASE WHEN %s THEN now() ELSE last_alerted_at END,
            updated_at       = now()
        WHERE id = 1
        """,
        (fingerprint, json.dumps(problems), alerted),
    )


def send_alert_email(problems: list[str]) -> None:
    """Email the actionable problem list. Raises on failure — caller decides."""
    from_address = os.environ["SES_FROM_ADDRESS"]
    to_address   = os.environ["ALERT_TO_ADDRESS"]

    body_lines = [
        "The hourly Asana agency board -> HubSpot sync hit failures that need a human:",
        "",
    ]
    for p in problems:
        body_lines.append(f"  * {p}")
    body_lines += [
        "",
        "How to fix:",
        "  - Unknown dropdown option: add the new Asana status as an option on the",
        "    HubSpot company property 'asana_agency_status'. The sync retries hourly",
        "    and heals itself once the option exists.",
        "  - Unmapped account manager: INSERT the name + HubSpot owner id into",
        "    account_manager_hubspot_map, then run:",
        "      UPDATE asana_agency_board_task SET last_synced_hash = NULL",
        "      WHERE account_manager = '<name>';",
        "    so their companies re-push with the owner set.",
        "",
        "Details: docs/system_reference.md -> 'HubSpot' section.",
        "You will only be emailed again if the problem set changes.",
    ]

    msg = MIMEText("\n".join(body_lines))
    msg["Subject"] = "[gymlaunch] Agency board -> HubSpot sync needs attention"
    msg["From"]    = from_address
    msg["To"]      = to_address

    boto3.client("ses", region_name="us-east-1").send_raw_email(
        Source=from_address,
        Destinations=[to_address],
        RawMessage={"Data": msg.as_string()},
    )
    print(f"  Alert emailed to {to_address}: {len(problems)} problem(s)")


def maybe_alert(cur, conn, errors_by_id: dict, unmapped_ams: set) -> dict:
    """
    Email when the actionable problem set changes. Never raises — an alerting
    failure must not break the sync. On send failure the fingerprint is left
    alone so the alert retries next run.
    """
    problems = build_problem_list(errors_by_id, unmapped_ams)
    fingerprint = (
        hashlib.sha256("\n".join(problems).encode()).hexdigest() if problems else ""
    )
    result = {"alert_sent": False, "alert_problems": problems}
    try:
        if fingerprint == fetch_alert_fingerprint(cur):
            return result
        if problems:
            send_alert_email(problems)
            result["alert_sent"] = True
        store_alert_state(cur, fingerprint, problems, result["alert_sent"])
        conn.commit()
    except Exception as e:
        # Don't advance the fingerprint — retry the alert next run.
        conn.rollback()
        print(f"  Warning: alerting failed (sync itself is unaffected): {e}")
        result["alert_error"] = str(e)[:300]
    return result


# --- Agency Pro handler ---

def clear_coach_property(company_id: str) -> None:
    """Clear the coach field on a HubSpot company record."""
    resp = hubspot_patch(
        f"/crm/v3/objects/companies/{company_id}",
        {"properties": {"coach": ""}},
    )
    if resp.status_code not in (200, 204):
        print(f"  Warning: could not clear coach for company {company_id}: {resp.status_code} {resp.text[:200]}")


# --- Sending ---

def update_single(company_id: str, props: dict) -> dict | None:
    """
    PATCH one company. Returns None on success, or
    {"error": <text>, "park": <bool>} on failure. 404s (dead/deleted company
    ids) are parkable — their fix lives in Asana; see mark_parked().
    """
    resp = hubspot_patch(f"/crm/v3/objects/companies/{company_id}", {"properties": props})
    if resp.status_code == 429:
        resp = hubspot_patch(f"/crm/v3/objects/companies/{company_id}", {"properties": props})
    if resp.status_code in (200, 204):
        return None
    return {"error": extract_hubspot_error(resp), "park": resp.status_code == 404}


def send_all(inputs: list[dict]) -> dict:
    """
    Send inputs to HubSpot in batches; fall back to per-record updates when a
    batch fails, so one poison record can't block the rest.
    Returns {company_id: {"error": text, "park": bool}} for failed records.
    """
    errors_by_id = {}

    for i in range(0, len(inputs), HUBSPOT_BATCH_SIZE):
        batch = inputs[i : i + HUBSPOT_BATCH_SIZE]
        print(f"  Sending batch {i // HUBSPOT_BATCH_SIZE + 1} ({len(batch)} records)")

        resp = hubspot_batch_update(batch)

        if resp.status_code == 429:
            # Already slept in _handle_rate_limit, retry this batch once
            print("  Retrying batch after rate limit")
            resp = hubspot_batch_update(batch)

        if resp.status_code in (200, 201):
            continue

        # Batch rejected (HubSpot batches are atomic on validation errors) or
        # partial — retry each record individually to isolate the bad ones.
        print(f"  Batch failed ({resp.status_code}), falling back to per-record updates: {resp.text[:300]}")
        for item in batch:
            err = update_single(item["id"], item["properties"])
            if err:
                print(f"    Company {item['id']} failed{' (parking)' if err['park'] else ''}: {err['error'][:200]}")
                errors_by_id[item["id"]] = err

    return errors_by_id


# --- Main sync ---

def sync_to_hubspot(conn) -> dict:
    cur = conn.cursor()

    rows = fetch_pending_rows(cur)
    am_map = fetch_am_map(cur)

    if not rows:
        print("No rows pending HubSpot sync")
        return {"pending": 0}

    print(f"{len(rows)} rows pending HubSpot sync")

    # Build inputs for batch update. Several Asana tasks can point at the same
    # company: dedupe what we SEND (last row wins) but track every row per
    # company so each one gets its status written back.
    inputs_by_id  = {}  # company_id -> batch input (deduped)
    rows_by_hs_id = {}  # company_id -> [rows]
    agency_pro    = []  # company IDs that need coach cleared
    unmapped_ams  = set()
    normalized    = {}  # raw value -> normalized id (for the debug response)

    for row in rows:
        am_id = am_map.get(row["account_manager"])
        if row["account_manager"] and not am_id:
            unmapped_ams.add(row["account_manager"])
            print(f"  Warning: no HubSpot ID found for AM '{row['account_manager']}' on task {row['task_gid']}")

        hs_id = normalize_company_id(row["hubspot_company_id"])
        if hs_id != row["hubspot_company_id"]:
            normalized[row["hubspot_company_id"]] = hs_id
            print(f"  Normalized company id '{row['hubspot_company_id'][:80]}' -> {hs_id}")

        props = build_properties(row, am_id)
        # Strip None values — HubSpot ignores missing keys, no need to send nulls
        props = {k: v for k, v in props.items() if v is not None}

        inputs_by_id[hs_id] = {
            "id":         hs_id,
            "properties": props,
        }
        rows_by_hs_id.setdefault(hs_id, []).append(row)

        if row.get("coach") == "Agency Pro":
            agency_pro.append(hs_id)

    inputs = list(inputs_by_id.values())

    if debug_mode():
        print("DEBUG mode — no HubSpot writes, no DB write-backs, no alert emails")
        return {
            "debug": True,
            "pending": len(rows),
            "unmapped_account_managers": sorted(unmapped_ams),
            # HubSpot rejections can't be known without sending; unmapped AMs
            # are the alert-worthy problems visible in a dry run.
            "would_alert_about": build_problem_list({}, unmapped_ams),
            "normalized_company_ids": normalized,
            "would_clear_coach_for": agency_pro,
            "would_send": inputs,
        }

    errors_by_id = send_all(inputs)

    # Write results back to DB — every row for a company, not just one
    parked = 0
    for hs_id, hs_rows in rows_by_hs_id.items():
        err = errors_by_id.get(hs_id)
        for row in hs_rows:
            if err is None:
                mark_success(cur, row["task_gid"], row["content_hash"])
            elif err["park"]:
                mark_parked(cur, row["task_gid"], row["content_hash"], err["error"])
                parked += 1
            else:
                mark_error(cur, row["task_gid"], err["error"])

    conn.commit()
    print(f"  DB updated: {len(rows_by_hs_id) - len(errors_by_id)} companies success, "
          f"{len(errors_by_id)} failed ({parked} rows parked)")

    # Clear coach for Agency Pro records
    if agency_pro:
        print(f"  Clearing coach for {len(agency_pro)} Agency Pro records")
        for company_id in agency_pro:
            clear_coach_property(company_id)

    # Email if the actionable problem set changed (never breaks the sync)
    alert = maybe_alert(cur, conn, errors_by_id, unmapped_ams)

    return {
        "pending": len(rows),
        "success": len(rows_by_hs_id) - len(errors_by_id),
        "failed": len(errors_by_id),
        "parked_rows": parked,
        "errors": {hs_id: err["error"][:200] for hs_id, err in errors_by_id.items()},
        "normalized_company_ids": normalized,
        "unmapped_account_managers": sorted(unmapped_ams),
        **alert,
    }


# --- Lambda entry point ---

def lambda_handler(event, context):
    print("HubSpot sync starting")

    # Manual test hook — sends a sample alert through the real SES path
    # without touching the DB, the fingerprint, or HubSpot:
    #   aws lambda invoke --function-name gymlaunch-sync-agency-board-to-hubspot \
    #     --payload '{"test_alert": true}' --cli-binary-format raw-in-base64-out out.json
    # (EventBridge scheduled events never carry this key, so cron runs are unaffected.)
    if isinstance(event, dict) and event.get("test_alert"):
        print("test_alert invoke — sending sample alert email")
        send_alert_email([
            "TEST ALERT — no real failure occurred. This verifies SES delivery.",
            "Example: HTTP 400: Some New Status was not one of the allowed options: "
            "[...]  [3 companies affected]",
            "Example: Account manager 'New Hire' has no row in "
            "account_manager_hubspot_map — their companies sync WITHOUT an owner",
        ])
        return {"statusCode": 200, "body": json.dumps({"test_alert_sent": True})}

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT pg_try_advisory_lock(hashtext('hubspot_sync'))")
        if not cur.fetchone()[0]:
            print("Another HubSpot sync is already running, exiting")
            return {"statusCode": 200, "body": "Skipped: sync already running"}

        result = sync_to_hubspot(conn)

        print("HubSpot sync complete")
        return {"statusCode": 200, "body": json.dumps(result, default=str)}

    except Exception as e:
        print(f"ERROR during HubSpot sync: {e}")
        raise

    finally:
        conn.close()
