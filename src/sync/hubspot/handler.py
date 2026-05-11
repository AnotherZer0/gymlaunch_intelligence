"""
HubSpot sync Lambda
Reads rows from asana_agency_board_task where content_hash differs from
last_synced_hash, pushes updates to HubSpot via the batch companies API,
then records the result back to the DB.

Runs at :10 past each hour, 10 minutes after the Asana sync.
"""

import os
import ssl
import time
from datetime import datetime, timezone, date

import pg8000
import requests


# --- Config ---

# Seconds to wait for each HubSpot API call before raising a timeout error.
# Increase if you see timeout errors; decrease for faster failure detection.
REQUEST_TIMEOUT_SECONDS = 30

HUBSPOT_BATCH_SIZE = 100  # HubSpot max per batch update call
HUBSPOT_BASE_URL   = "https://api.hubapi.com"


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
    """
    return {
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


# --- Agency Pro handler ---

def clear_coach_property(company_id: str) -> None:
    """Clear the coach field on a HubSpot company record."""
    resp = hubspot_patch(
        f"/crm/v3/objects/companies/{company_id}",
        {"properties": {"coach": ""}},
    )
    if resp.status_code not in (200, 204):
        print(f"  Warning: could not clear coach for company {company_id}: {resp.status_code} {resp.text[:200]}")


# --- Main sync ---

def sync_to_hubspot(conn) -> None:
    cur = conn.cursor()

    rows = fetch_pending_rows(cur)
    am_map = fetch_am_map(cur)

    if not rows:
        print("No rows pending HubSpot sync")
        return

    print(f"{len(rows)} rows pending HubSpot sync")

    # Build inputs for batch update, keyed by task_gid for result tracking
    inputs       = []
    row_by_hs_id = {}  # hubspot_company_id -> row (for result mapping)
    agency_pro   = []  # company IDs that need coach cleared

    for row in rows:
        am_id = am_map.get(row["account_manager"])
        if row["account_manager"] and not am_id:
            print(f"  Warning: no HubSpot ID found for AM '{row['account_manager']}' on task {row['task_gid']}")

        props = build_properties(row, am_id)
        # Strip None values — HubSpot ignores missing keys, no need to send nulls
        props = {k: v for k, v in props.items() if v is not None}

        inputs.append({
            "id":         row["hubspot_company_id"],
            "properties": props,
        })
        row_by_hs_id[row["hubspot_company_id"]] = row

        if row.get("coach") == "Agency Pro":
            agency_pro.append(row["hubspot_company_id"])

    # Send in batches of HUBSPOT_BATCH_SIZE
    failed_ids = set()
    for i in range(0, len(inputs), HUBSPOT_BATCH_SIZE):
        batch = inputs[i : i + HUBSPOT_BATCH_SIZE]
        print(f"  Sending batch {i // HUBSPOT_BATCH_SIZE + 1} ({len(batch)} records)")

        resp = hubspot_batch_update(batch)

        if resp.status_code == 429:
            # Already slept in _handle_rate_limit, retry this batch once
            print("  Retrying batch after rate limit")
            resp = hubspot_batch_update(batch)

        if resp.status_code not in (200, 201, 207):
            print(f"  Batch failed: {resp.status_code} {resp.text[:300]}")
            for item in batch:
                failed_ids.add(item["id"])
        else:
            # 207 means partial success — check individual results
            if resp.status_code == 207:
                data = resp.json()
                for result in data.get("errors", []):
                    failed_ids.add(result.get("id"))

    # Write results back to DB
    for hs_id, row in row_by_hs_id.items():
        if hs_id in failed_ids:
            mark_error(cur, row["task_gid"], f"batch update failed for company {hs_id}")
        else:
            mark_success(cur, row["task_gid"], row["content_hash"])

    conn.commit()
    print(f"  DB updated: {len(row_by_hs_id) - len(failed_ids)} success, {len(failed_ids)} failed")

    # Clear coach for Agency Pro records
    if agency_pro:
        print(f"  Clearing coach for {len(agency_pro)} Agency Pro records")
        for company_id in agency_pro:
            clear_coach_property(company_id)


# --- Lambda entry point ---

def lambda_handler(event, context):
    print("HubSpot sync starting")

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT pg_try_advisory_lock(hashtext('hubspot_sync'))")
        if not cur.fetchone()[0]:
            print("Another HubSpot sync is already running, exiting")
            return {"statusCode": 200, "body": "Skipped: sync already running"}

        sync_to_hubspot(conn)

        print("HubSpot sync complete")
        return {"statusCode": 200, "body": "Sync complete"}

    except Exception as e:
        print(f"ERROR during HubSpot sync: {e}")
        raise

    finally:
        conn.close()
