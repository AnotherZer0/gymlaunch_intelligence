"""
Lead-DB2 sheet sync Lambda (gymlaunch-lead_db2-sheet-sync)

Daily mirror of one Google Sheets tab into RDS — the master client table that
drives the FB-lead pipeline's monitoring layer.

================================================================================
What this Lambda does
================================================================================

Reads the `00 - Database` tab from the Google Sheet `Lead Integration Database
V2.0` (document ID `1JGdbjR1g8MF0zzraOwyPNNY7-jRrVIzk2ZZI-FWhky0`) once per day
at 07:00 UTC (one hour before `gymlaunch-supabase-lead-sync`), and upserts every
row into the RDS table `client_lead_master`.

Per-run summary is recorded in the existing `supabase_sync_state` table under
`table_name = 'client_lead_master'`. Despite that table's name, it's used as the
generic per-table sync watermark across all our sync Lambdas.

================================================================================
Why this Lambda exists — read this before you touch it
================================================================================

The `00 - Database` sheet is the operational client master for the FB-lead
integration. Every client (FB page → GHL location pairing) has one row in it.
That sheet is touched by TWO things:

1. **The n8n workflow `00 - Main Workflow`** (the load-bearing FB-leadgen
   webhook receiver) writes 12 columns automatically on every FB lead arrival,
   using `appendOrUpdate` keyed on `facebook_page_id`:
     - facebook_page_name, facebook_page_id, facebook_ad_account_id,
       facebook_ad_account_name, hl_sub_account_location_id,
       hl_sub_account_location_name, hl_sub_account_api_key, asana_task_id,
       gym_name, client_name, asana_status, workflow_status

2. **Humans** maintain ten further diagnostic columns by hand:
     - system_user           ← Yes/No, "do we have FB BM access for this client"
     - fb_app_status         ← used to filter the Non-System-User heatmap
     - ghl_connection        ← which agency GHL account is wired to this client
     - workflow_connection   ← e.g. "Make" / "No"
     - page_connection       ← e.g. "Personal - Alex Burner"
     - lead_access_issue
     - ghl_snapshot
     - notes
     - lead_forms
     - supabase              ← Yes/No flag — is this client in the new pipeline

The phase-2 compare sheet that goes to the team (`1TR2SQxt...`) needs to
display `system_user` (and likely other diagnostic flags) per row. The sheet
above is where those values live; the n8n workflow doesn't write them.

We mirror the WHOLE sheet — including `hl_sub_account_api_key` as of
migration 011 — so all manual flags AND the per-location GHL keys are
queryable in SQL, joinable with the lead tables, and usable by future
per-client GHL automation. The humans keep editing the sheet exactly as
before; this Lambda is purely read-only on the sheet.

Tracked-for-future hardening: `docs/future_work.md` ("Harden GHL API key
storage"). If/when that work lands, the api_key column moves to Secrets
Manager and this Lambda stops mirroring it.

================================================================================
Why this is a separate Lambda from gymlaunch-supabase-lead-sync
================================================================================

- Different source system (Google Sheets vs Supabase REST).
- Different failure modes (sheet rename / sharing revocation vs API auth).
- Runs 1 hour earlier so the supabase-lead-sync (and any phase-2 compare) sees
  a fresh client_lead_master snapshot.
- Easier per-Lambda CloudWatch monitoring + retries.
- Smaller blast radius — a sheet permission breakage doesn't kill the lead
  mirror, and a Supabase outage doesn't block the client master refresh.

================================================================================
Future work — replacing the n8n workflow
================================================================================

The upstream `00 - Main Workflow` is the actual load-bearing FB-leads-into-GHL
integration, not just monitoring. If it dies, customers stop receiving leads in
their CRM. The plan to replace it (post-phase-2) is to stand up our own webhook
receivers for FB leadgen + GHL contact events, write directly to RDS, and
subscribe each FB page / GHL location to our endpoints instead of n8n's.

If/when that lands, the `00 - Database` sheet may go away. At that point this
Lambda becomes obsolete or pivots to syncing FROM RDS BACK TO a sheet (so the
team retains a familiar UI for human-edited columns).

================================================================================
Operational notes
================================================================================

- The Google service account `n8n-sheets-integration@zsign-transfer.iam.gserviceaccount.com`
  must have Viewer access (or higher) on the source sheet. If the sync starts
  failing with a 403, check the sheet's share list.
- Header row IS the first row of the sheet (no merged title row). If someone
  inserts a title row at the top, the sync will silently mis-map columns —
  guard added below verifies the expected header set.
- Empty rows (no facebook_page_id) are skipped without erroring.
- Re-running is safe — every row is an UPSERT on facebook_page_id, so the
  outcome is idempotent.
"""

import base64
import json
import os
import ssl
import time

import gspread
import pg8000
from google.oauth2.service_account import Credentials


# --- Config ---

SHEET_TAB_NAME       = "00 - Database"
RDS_TABLE_NAME       = "client_lead_master"
SCOPES               = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
ADVISORY_LOCK_KEY    = "lead_db2_sheet_sync"
DB_UPSERT_BATCH_SIZE = 200

# Columns we mirror from the sheet → RDS. Order matters: this is the INSERT column list.
# `hl_sub_account_api_key` is included as of migration 011 — see docs/future_work.md
# entry "Harden GHL API key storage" for the path to move it to Secrets Manager.
RDS_COLUMNS = [
    "facebook_page_id",            # PK
    "facebook_page_name",
    "facebook_ad_account_id",
    "facebook_ad_account_name",
    "hl_sub_account_location_id",
    "hl_sub_account_location_name",
    "hl_sub_account_api_key",
    "asana_task_id",
    "gym_name",
    "client_name",
    "asana_status",
    "workflow_status",
    "ghl_connection",
    "workflow_connection",
    "page_connection",
    "lead_access_issue",
    "fb_app_status",
    "is_system_user",              # sheet header is `system_user` (SQL reserved keyword in PG 16+)
    "ghl_snapshot",
    "notes",
    "lead_forms",
    "supabase",
]

# RDS column name → sheet header. Only listed when they differ.
SHEET_HEADER_OVERRIDES = {
    "is_system_user": "system_user",
}

def sheet_header_for(rds_column: str) -> str:
    return SHEET_HEADER_OVERRIDES.get(rds_column, rds_column)

# Sheet column headers we EXPECT to find. If the human editor renames a column
# in the sheet, we want to fail loudly rather than silently land NULLs.
REQUIRED_SHEET_HEADERS = {sheet_header_for(c) for c in RDS_COLUMNS}

PK_COLUMN = "facebook_page_id"


# --- DB ---

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


# --- Google Sheets ---

def get_sheets_client():
    sa_json = json.loads(base64.b64decode(os.environ["GOOGLE_SERVICE_ACCOUNT_B64"]))
    creds = Credentials.from_service_account_info(sa_json, scopes=SCOPES)
    return gspread.authorize(creds)


def fetch_sheet_rows():
    """Returns a list of dicts (one per data row), keyed by header name.
    Verifies the sheet has the expected header set; raises if a required column
    is missing so a silent column rename surfaces immediately."""
    gc = get_sheets_client()
    sh = gc.open_by_key(os.environ["LEAD_DB2_SHEET_ID"])
    ws = sh.worksheet(SHEET_TAB_NAME)
    rows = ws.get_all_records()  # uses row 1 as headers
    if not rows:
        return []
    seen_headers = set(rows[0].keys())
    missing = REQUIRED_SHEET_HEADERS - seen_headers
    if missing:
        raise RuntimeError(
            f"Sheet {SHEET_TAB_NAME!r} is missing expected header columns: "
            f"{sorted(missing)}. Found headers: {sorted(seen_headers)}"
        )
    return rows


# --- Upsert ---

def upsert_rows(cur, rows: list[dict]):
    """Batched upsert by facebook_page_id."""
    if not rows:
        return
    placeholders = "(" + ",".join(["%s"] * len(RDS_COLUMNS)) + ")"
    values_clause = ",".join([placeholders] * len(rows))
    update_clause = ",".join([
        f"{col} = EXCLUDED.{col}" for col in RDS_COLUMNS if col != PK_COLUMN
    ]) + ", synced_at = now()"
    sql = (
        f"INSERT INTO {RDS_TABLE_NAME} ({','.join(RDS_COLUMNS)}) "
        f"VALUES {values_clause} "
        f"ON CONFLICT ({PK_COLUMN}) DO UPDATE SET {update_clause}"
    )
    params = []
    for row in rows:
        for col in RDS_COLUMNS:
            v = row.get(sheet_header_for(col))
            # gspread returns "" for empty cells; convert to NULL for cleaner SQL
            if v == "":
                v = None
            params.append(v)
    cur.execute(sql, params)


def record_sync_state(cur, row_count: int, status: str, error: str | None):
    cur.execute(
        """
        INSERT INTO supabase_sync_state
            (table_name, last_synced_at, last_row_count, last_status, last_error, updated_at)
        VALUES (%s, now(), %s, %s, %s, now())
        ON CONFLICT (table_name) DO UPDATE SET
            last_synced_at = EXCLUDED.last_synced_at,
            last_row_count = EXCLUDED.last_row_count,
            last_status    = EXCLUDED.last_status,
            last_error     = EXCLUDED.last_error,
            updated_at     = now()
        """,
        (RDS_TABLE_NAME, row_count, status, error),
    )


# --- Entry point ---

def lambda_handler(event, context):
    print("lead_db2 sheet sync starting")
    started  = time.time()
    conn     = get_db_connection()
    got_lock = False
    try:
        # Advisory lock — prevent overlap if a manual invoke races with the cron
        cur = conn.cursor()
        cur.execute(
            "SELECT pg_try_advisory_lock(hashtext(%s))",
            (ADVISORY_LOCK_KEY,),
        )
        got_lock = bool(cur.fetchone()[0])
        cur.close()
        if not got_lock:
            print("Another lead_db2 sheet sync is running, exiting")
            return {"statusCode": 200, "body": "already running"}

        try:
            rows = fetch_sheet_rows()
        except Exception as e:
            print(f"FAILED to fetch sheet: {e}")
            cur = conn.cursor()
            record_sync_state(cur, 0, "error", f"fetch_failed: {str(e)[:600]}")
            conn.commit()
            cur.close()
            return {"statusCode": 500, "body": f"sheet fetch failed: {e}"}

        # Skip empty / unkeyed rows
        valid_rows = [r for r in rows if (r.get(PK_COLUMN) or "").strip()]
        skipped    = len(rows) - len(valid_rows)
        print(f"Fetched {len(rows)} row(s) from sheet, {len(valid_rows)} valid "
              f"(skipped {skipped} empty)")

        upserted = 0
        try:
            for i in range(0, len(valid_rows), DB_UPSERT_BATCH_SIZE):
                batch = valid_rows[i : i + DB_UPSERT_BATCH_SIZE]
                cur = conn.cursor()
                upsert_rows(cur, batch)
                conn.commit()
                cur.close()
                upserted += len(batch)

            cur = conn.cursor()
            record_sync_state(cur, upserted, "success", None)
            conn.commit()
            cur.close()
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            cur = conn.cursor()
            record_sync_state(cur, upserted, "error", str(e)[:1000])
            conn.commit()
            cur.close()
            raise

        elapsed = time.time() - started
        summary = {
            "elapsed_seconds": round(elapsed, 1),
            "fetched":         len(rows),
            "skipped_empty":   skipped,
            "upserted":        upserted,
        }
        print(f"lead_db2 sheet sync complete: {summary}")
        return {"statusCode": 200, "body": summary}
    finally:
        if got_lock:
            try:
                cur = conn.cursor()
                cur.execute(
                    "SELECT pg_advisory_unlock(hashtext(%s))",
                    (ADVISORY_LOCK_KEY,),
                )
                cur.close()
            except Exception:
                pass
        try:
            conn.close()
        except Exception:
            pass
