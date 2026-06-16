"""
Supabase lead sync Lambda (gymlaunch-supabase-lead-sync)

Daily mirror of 7 Supabase tables into RDS. Source is the Supabase project at
lvidkcbzjvzuxwhtmndq.supabase.co, accessed via the PostgREST API using the
service_role JWT (in env as SUPABASE_SERVICE_ROLE_KEY).

Why REST and not direct Postgres:
  The Supabase project's database password isn't recoverable from the dashboard
  and is suspected to be hardcoded in an upstream n8n workflow. Rotating it
  would risk breaking that workflow. The service_role JWT is a separate auth
  mechanism and doesn't conflict with the DB password.

Tables mirrored (Supabase → RDS):
  02 - Facebook Leads         → supabase_facebook_lead
  03 - HighLevel Leads        → supabase_highlevel_lead
  01 - Lead Form Database     → supabase_lead_form
  lead_forms                  → supabase_lead_form_detail
  Facebook Form Database      → supabase_facebook_form
  Facebook Lead Form Database → supabase_facebook_lead_form
  Facebook Leads Database     → supabase_facebook_lead_legacy

Not mirrored:
  "00 - Lead Integration Main Database" — same per-client metadata is already
  captured in asana_agency_board_task (gym_name, client_name, facebook_page_id,
  facebook_ad_account_id, hl_sub_account_location_id, hubspot_company_id, AM,
  MB, coach). The Supabase Main table also carries hl_sub_account_api_key in
  plaintext, which we don't want to propagate.

Strategy:
  Full pull + upsert by PK every night. The user's original ask was a 7-day
  window for the lead tables, but `created_at` in those tables is TEXT in
  M/D/YYYY format which doesn't sort lexically — so a clean server-side
  range filter isn't available. Pulling everything is the simplest correct
  alternative and well within Lambda budget at current scale
  (~70k rows × 2 lead tables ≈ ~1 minute total).

Phase 2 (not yet built):
  Compare view (FB count vs GHL count per client per day), Google Sheet write,
  integration-break threshold logic. The mirror tables and indices in this
  Lambda's migration are already sized for that work.

Failure model:
  Per-table failures are isolated — a 500 from Supabase or a constraint
  violation on one table doesn't abort the other six. State table records
  per-table success/error. If ANY table failed the Lambda returns a non-200
  status code so a CloudWatch alarm can spot it.
"""

import json
import os
import ssl
import time
from datetime import datetime
from typing import Iterable
from urllib.parse import quote

import pg8000
import requests


# --- Config ---

REQUEST_TIMEOUT_SECONDS = 60
SUPABASE_PAGE_SIZE      = 1000          # PostgREST default max
DB_UPSERT_BATCH_SIZE    = 500           # rows per INSERT statement
ADVISORY_LOCK_KEY       = "supabase_lead_sync"


# --- Helpers ---

def parse_mdy(value):
    """Parse M/D/YYYY text into a `datetime.date`. None on empty/unparseable."""
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), "%m/%d/%Y").date()
    except (ValueError, AttributeError):
        return None


def json_dump(value):
    """Serialize a Python value to a JSON string for JSONB columns.
    Pass-through for None; pg8000 will land NULL."""
    if value is None:
        return None
    return json.dumps(value)


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


# --- Supabase REST ---

def supabase_headers():
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    return {
        "apikey":        key,
        "Authorization": f"Bearer {key}",
    }


def supabase_fetch_all(table_name: str) -> Iterable[dict]:
    """Yield all rows from a Supabase table via PostgREST, paginated.

    Uses 0-indexed inclusive Range headers (PostgREST's standard pagination).
    Iteration stops on:
      - status 416 Range Not Satisfiable (asked past end of data)
      - empty results
      - partial page (len < SUPABASE_PAGE_SIZE)
    Retries once on transient 429/503 with the Retry-After hint.
    """
    base_url = os.environ["SUPABASE_URL"].rstrip("/")
    # Table names with spaces / dashes / leading digits (e.g. "02 - Facebook Leads")
    # need URL-encoding for the path segment.
    url = f"{base_url}/rest/v1/{quote(table_name, safe='')}"
    offset = 0
    page = 0
    while True:
        page += 1
        headers = {
            **supabase_headers(),
            "Range-Unit": "items",
            "Range":      f"{offset}-{offset + SUPABASE_PAGE_SIZE - 1}",
            "Prefer":     "count=none",
        }
        resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)

        if resp.status_code in (429, 503):
            wait = int(resp.headers.get("Retry-After", 5))
            print(f"  [{table_name}] {resp.status_code} from Supabase, waiting {wait}s")
            time.sleep(wait)
            continue

        if resp.status_code == 416:
            print(f"  [{table_name}] range past end at offset={offset}, done")
            return

        if resp.status_code not in (200, 206):
            raise RuntimeError(
                f"Supabase fetch failed for {table_name!r}: "
                f"{resp.status_code} {resp.text[:300]}"
            )

        rows = resp.json()
        if not isinstance(rows, list):
            raise RuntimeError(
                f"Unexpected non-list response from Supabase for {table_name!r}: "
                f"{str(rows)[:300]}"
            )

        print(f"  [{table_name}] page {page}: {len(rows)} row(s) (offset={offset})")
        if not rows:
            return
        yield from rows

        if len(rows) < SUPABASE_PAGE_SIZE:
            return
        offset += SUPABASE_PAGE_SIZE


# --- Upsert ---

def upsert_rows(cur, rds_table: str, columns: list[str], pk: str, rows: list[dict]):
    """Batched upsert: one INSERT with N value-tuples, ON CONFLICT update all
    non-PK columns. Column and table identifiers are NOT user input — they're
    hard-coded in SYNC_TABLES below — so f-string composition is safe here."""
    if not rows:
        return
    placeholders = "(" + ",".join(["%s"] * len(columns)) + ")"
    values_clause = ",".join([placeholders] * len(rows))
    update_clause = ",".join([
        f"{col} = EXCLUDED.{col}" for col in columns if col != pk
    ])
    sql = (
        f"INSERT INTO {rds_table} ({','.join(columns)}) VALUES {values_clause} "
        f"ON CONFLICT ({pk}) DO UPDATE SET {update_clause}"
    )
    params = []
    for row in rows:
        for col in columns:
            params.append(row.get(col))
    cur.execute(sql, params)


def record_sync_state(cur, table_name: str, row_count: int,
                      status: str, error: str | None):
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
        (table_name, row_count, status, error),
    )


# --- Per-table transforms ---
#
# Each transform takes a Supabase row dict and returns a dict whose keys match
# the RDS column list for that table. Missing source keys land as None (NULL).

def transform_lead_row(row: dict) -> dict:
    """Shared by supabase_facebook_lead and supabase_highlevel_lead."""
    return {
        "lead_id":           row.get("lead_id"),
        "page_id":           row.get("page_id"),
        "page_name":         row.get("page_name"),
        "source":            row.get("source"),
        "sub_account_id":    row.get("sub_account_id"),
        "sub_account_name":  row.get("sub_account_name"),
        "form_id":           row.get("form_id"),
        "form_name":         row.get("form_name"),
        "first_name":        row.get("first_name"),
        "last_name":         row.get("last_name"),
        "email":             row.get("email"),
        "phone":             row.get("phone"),
        "execution_id_link": row.get("execution_id_link"),
        "lead_date":         parse_mdy(row.get("created_at")),
        "created_at_raw":    row.get("created_at"),
    }


def transform_lead_form(row: dict) -> dict:
    return {
        "form_id":            row.get("form_id"),
        "form_name":          row.get("form_name"),
        "facebook_page_id":   row.get("facebook_page_id"),
        "facebook_page_name": row.get("facebook_page_name"),
        "create_date_raw":    row.get("create_date"),
        "date_added_raw":     row.get("date_added"),
    }


def transform_lead_form_detail(row: dict) -> dict:
    return {
        "form_id":               row.get("form_id"),
        "page_id":               row.get("page_id"),
        "client_name":           row.get("client_name"),
        "name":                  row.get("name"),
        "status":                row.get("status"),
        "locale":                row.get("locale"),
        "created_time":          row.get("created_time"),
        "leads_count":           row.get("leads_count"),
        "questions":             json_dump(row.get("questions")),
        "context_card":          json_dump(row.get("context_card")),
        "thank_you_page":        json_dump(row.get("thank_you_page")),
        "follow_up_action_url":  row.get("follow_up_action_url"),
        "raw":                   json_dump(row.get("raw")),
        "last_synced_at_source": row.get("last_synced_at"),
    }


def transform_facebook_form(row: dict) -> dict:
    return {
        "facebook_page_id":   row.get("facebook_page_id"),
        "facebook_page_name": row.get("facebook_page_name"),
        "form_id":            row.get("form_id"),
        "form_name":          row.get("form_name"),
        "create_date":        row.get("create_date"),
        "date_added":         row.get("date_added"),
        "is_active":          row.get("is_active"),
    }


def transform_facebook_lead_form(row: dict) -> dict:
    return {
        "id":         row.get("id"),
        "created_at": row.get("created_at"),
    }


def transform_facebook_lead_legacy(row: dict) -> dict:
    return {
        "facebook_leadgen_id":      row.get("facebook_leadgen_id"),
        "facebook_page_id":         row.get("facebook_page_id"),
        "facebook_form_id":         row.get("facebook_form_id"),
        "facebook_lead_first_name": row.get("facebook_lead_first_name"),
        "facebook_lead_last_name":  row.get("facebook_lead_last_name"),
        "facebook_lead_email":      row.get("facebook_lead_email"),
        "facebook_lead_phone":      row.get("facebook_lead_phone"),
        "system_execution_id_url":  row.get("system_execution_id_url"),
        "system_status":            row.get("system_status"),
        "system_created_at":        row.get("system_created_at"),
    }


# --- Sync plan ---
#
# Order: critical lead tables first, reference next, legacy last. Iteration
# below is fault-tolerant — a single failure doesn't abort the rest.

SYNC_TABLES = [
    {
        "supabase_name": "02 - Facebook Leads",
        "rds_table":     "supabase_facebook_lead",
        "pk":            "lead_id",
        "transform":     transform_lead_row,
        "columns": [
            "lead_id", "page_id", "page_name", "source",
            "sub_account_id", "sub_account_name",
            "form_id", "form_name",
            "first_name", "last_name", "email", "phone",
            "execution_id_link", "lead_date", "created_at_raw",
        ],
    },
    {
        "supabase_name": "03 - HighLevel Leads",
        "rds_table":     "supabase_highlevel_lead",
        "pk":            "lead_id",
        "transform":     transform_lead_row,
        "columns": [
            "lead_id", "page_id", "page_name", "source",
            "sub_account_id", "sub_account_name",
            "form_id", "form_name",
            "first_name", "last_name", "email", "phone",
            "execution_id_link", "lead_date", "created_at_raw",
        ],
    },
    {
        "supabase_name": "01 - Lead Form Database",
        "rds_table":     "supabase_lead_form",
        "pk":            "form_id",
        "transform":     transform_lead_form,
        "columns": [
            "form_id", "form_name",
            "facebook_page_id", "facebook_page_name",
            "create_date_raw", "date_added_raw",
        ],
    },
    {
        "supabase_name": "lead_forms",
        "rds_table":     "supabase_lead_form_detail",
        "pk":            "form_id",
        "transform":     transform_lead_form_detail,
        "columns": [
            "form_id", "page_id", "client_name", "name", "status", "locale",
            "created_time", "leads_count",
            "questions", "context_card", "thank_you_page",
            "follow_up_action_url", "raw", "last_synced_at_source",
        ],
    },
    {
        "supabase_name": "Facebook Form Database",
        "rds_table":     "supabase_facebook_form",
        "pk":            "facebook_page_id",
        "transform":     transform_facebook_form,
        "columns": [
            "facebook_page_id", "facebook_page_name",
            "form_id", "form_name",
            "create_date", "date_added", "is_active",
        ],
    },
    {
        "supabase_name": "Facebook Lead Form Database",
        "rds_table":     "supabase_facebook_lead_form",
        "pk":            "id",
        "transform":     transform_facebook_lead_form,
        "columns":       ["id", "created_at"],
    },
    {
        "supabase_name": "Facebook Leads Database",
        "rds_table":     "supabase_facebook_lead_legacy",
        "pk":            "facebook_leadgen_id",
        "transform":     transform_facebook_lead_legacy,
        "columns": [
            "facebook_leadgen_id", "facebook_page_id", "facebook_form_id",
            "facebook_lead_first_name", "facebook_lead_last_name",
            "facebook_lead_email", "facebook_lead_phone",
            "system_execution_id_url", "system_status",
            "system_created_at",
        ],
    },
]


def sync_table(conn, config: dict) -> tuple[int, int]:
    """Pull a single Supabase table and upsert into RDS.
    Returns (rows_fetched, rows_upserted). Commits per chunk so a partial
    failure leaves already-written rows in place."""
    src      = config["supabase_name"]
    rds      = config["rds_table"]
    pk       = config["pk"]
    fxn      = config["transform"]
    cols     = config["columns"]
    started  = time.time()

    print(f"Syncing {src!r} → {rds}")
    fetched  = 0
    upserted = 0
    skipped_no_pk = 0
    try:
        batch = []
        for row in supabase_fetch_all(src):
            fetched += 1
            transformed = fxn(row)
            if transformed.get(pk) in (None, ""):
                skipped_no_pk += 1
                continue
            batch.append(transformed)
            if len(batch) >= DB_UPSERT_BATCH_SIZE:
                cur = conn.cursor()
                upsert_rows(cur, rds, cols, pk, batch)
                conn.commit()
                cur.close()
                upserted += len(batch)
                batch = []
        if batch:
            cur = conn.cursor()
            upsert_rows(cur, rds, cols, pk, batch)
            conn.commit()
            cur.close()
            upserted += len(batch)

        cur = conn.cursor()
        record_sync_state(cur, rds, upserted, "success", None)
        conn.commit()
        cur.close()

        elapsed = time.time() - started
        print(f"  done {rds}: fetched={fetched} upserted={upserted} "
              f"skipped_no_pk={skipped_no_pk} in {elapsed:.1f}s")
        return fetched, upserted
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        try:
            cur = conn.cursor()
            record_sync_state(cur, rds, upserted, "error", str(e)[:1000])
            conn.commit()
            cur.close()
        except Exception:
            pass
        raise


# --- Entry point ---

def lambda_handler(event, context):
    print("Supabase lead sync starting")
    started = time.time()

    conn = get_db_connection()
    got_lock = False
    try:
        # Advisory lock — prevents two runs (cron + manual invoke) from racing.
        cur = conn.cursor()
        cur.execute(
            "SELECT pg_try_advisory_lock(hashtext(%s))",
            (ADVISORY_LOCK_KEY,),
        )
        got_lock = bool(cur.fetchone()[0])
        cur.close()
        if not got_lock:
            print("Another Supabase lead sync is running, exiting")
            return {"statusCode": 200, "body": "already running"}

        results = {}
        errors  = {}
        for config in SYNC_TABLES:
            rds = config["rds_table"]
            try:
                fetched, upserted = sync_table(conn, config)
                results[rds] = {"fetched": fetched, "upserted": upserted}
            except Exception as e:
                errors[rds] = str(e)[:300]
                print(f"  FAILED {rds}: {e}")

        elapsed = time.time() - started
        summary = {
            "elapsed_seconds": round(elapsed, 1),
            "results":         results,
            "errors":          errors,
        }
        print(f"Supabase lead sync complete: {summary}")
        status_code = 500 if errors else 200
        return {"statusCode": status_code, "body": summary}
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
