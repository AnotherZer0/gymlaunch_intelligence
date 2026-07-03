"""
gymlaunch-sync-sf-billing-info-to-hubspot — push SF billing fields onto HubSpot companies.

Runs after the daily SF->RDS sync (~06:45 UTC). Computes 6 billing fields per company
from the sf_* tables and writes them to the matching HubSpot **company** (keyed by
sf_customer.hubspot_id). Only companies whose values CHANGED since last push are written
(hash compared against sf_hubspot_push_state) — so daily API load is tiny and we don't
spam HubSpot property history. No HubSpot reads.

FIELDS (company properties, written by internal name)
  billing_status         (dropdown)  Current / Past Due / Pending / Cancelled
  outstanding_balance    (number)    Σ closing_balance on Overdue + Partially-Paid invoices
  current_billing_amount (number)    latest recurring core-product invoice total (PIF-aware)
  billing_frequency      (string)    Weekly/Monthly/Quarterly/Annual/PIF, parsed from the name
  last_payment_date      (date)      latest successful (Paid) Payment transaction date
  next_payment_date      (date)      soonest upcoming bill date across Active subs

SCOPE: billing-active companies only (>= 1 subscription or invoice). Companies whose
hubspot_id isn't a numeric HubSpot id are skipped (the known SF<->HubSpot id-mismatch;
out of scope to fix here).

PRODUCT / FREQUENCY are Path A (text-based): product category from the invoice
`description` prefix, cadence from a keyword in the name. Robust product_id/plan_price
categorization is deferred (would need detail-fetching) — see docs/future_work.md.

ENV
  HUBSPOT_TOKEN  private-app token (companies write scope) + DB_* vars.
  DEBUG=1        dry run: compute + report a sample and the changed-count, write NOTHING
                 to HubSpot and touch no state.
"""

import datetime
import hashlib
import json
import os
import ssl

import pg8000
import requests

HUBSPOT_API = "https://api.hubapi.com"
BATCH_SIZE = 100          # HubSpot batch-update max inputs per call
HTTP_TIMEOUT = 30
SAFETY_MS = 60_000        # stop + checkpoint when under this much wall-clock remains

# Products that DON'T count toward Current Billing Amount (matched by name via the invoice
# description prefix, since product_id isn't synced). Names are looked up from sf_product.
EXCLUDED_PRODUCT_IDS = (
    "f9496295-4856-4768-a95c-b1b42905c885",   # Additional Programs
    "d09a9055-9f3b-4ae2-b570-e65dd0691ef5",   # Testing
)

DEBUG = os.environ.get("DEBUG", "").strip().lower() in ("1", "true", "yes", "on")

COLUMNS = ["hubspot_id", "billing_status", "outstanding_balance", "current_billing_amount",
           "billing_frequency", "last_payment_date", "next_payment_date"]

# Per-company billing rollup. Everything groups to the HubSpot company via
# sf_customer.hubspot_id (numeric ids only). See module docstring for the field rules.
BILLING_SQL = """
WITH cust AS (
    SELECT id AS customer_id, hubspot_id
    FROM sf_customer
    WHERE hubspot_id ~ '^[0-9]+$'
),
excluded AS (
    SELECT name FROM sf_product
    WHERE id IN ('f9496295-4856-4768-a95c-b1b42905c885',
                 'd09a9055-9f3b-4ae2-b570-e65dd0691ef5')
      AND name IS NOT NULL
),
sub AS (
    SELECT c.hubspot_id,
           bool_or(s.status = 'Active')    AS has_active,
           bool_or(s.status = 'Suspended') AS has_suspended,
           bool_or(s.status = 'Pending')   AS has_pending,
           min(s.next_bill_date) FILTER (
               WHERE s.status = 'Active' AND s.next_bill_date >= current_date
           ) AS next_payment_date
    FROM sf_subscription s
    JOIN cust c ON c.customer_id = s.customer_id
    GROUP BY c.hubspot_id
),
inv AS (
    SELECT c.hubspot_id,
           COALESCE(sum(i.closing_balance)
               FILTER (WHERE i.status IN ('Overdue','Partially Paid')), 0) AS outstanding_balance,
           bool_or(i.status IN ('Overdue','Partially Paid')) AS has_past_due
    FROM sf_invoice i
    JOIN cust c ON c.customer_id = i.customer_id
    GROUP BY c.hubspot_id
),
cur AS (
    SELECT DISTINCT ON (c.hubspot_id)
           c.hubspot_id,
           i.total_amount AS current_billing_amount,
           i.description  AS cur_desc,
           i.is_oneoff
    FROM sf_invoice i
    JOIN cust c ON c.customer_id = i.customer_id
    WHERE i.status NOT IN ('Projected','Void')
      AND split_part(COALESCE(i.description, ''), ' -> ', 1) NOT IN (SELECT name FROM excluded)
    ORDER BY c.hubspot_id, i.is_oneoff ASC, i.invoice_date DESC NULLS LAST, i.created_at DESC NULLS LAST
),
pay AS (
    SELECT c.hubspot_id, max(t.date) AS last_payment_date
    FROM sf_transaction t
    JOIN cust c ON c.customer_id = t.customer_id
    WHERE t.transaction_category = 'Payment' AND t.status = 'Paid'
    GROUP BY c.hubspot_id
),
base AS (
    SELECT hubspot_id FROM sub
    UNION
    SELECT hubspot_id FROM inv
)
SELECT b.hubspot_id,
       CASE
         WHEN COALESCE(sub.has_active, false) AND COALESCE(inv.has_past_due, false) THEN 'Past Due'
         WHEN COALESCE(sub.has_active, false) THEN 'Current'
         WHEN COALESCE(sub.has_suspended, false) THEN 'Past Due'
         WHEN COALESCE(sub.has_pending, false) THEN 'Pending'
         ELSE 'Cancelled'
       END AS billing_status,
       COALESCE(inv.outstanding_balance, 0) AS outstanding_balance,
       cur.current_billing_amount,
       CASE
         WHEN cur.is_oneoff THEN 'PIF'
         WHEN cur.cur_desc ILIKE '%quarter%' THEN 'Quarterly'
         WHEN cur.cur_desc ILIKE '%week%'    THEN 'Weekly'
         WHEN cur.cur_desc ILIKE '%month%' OR cur.cur_desc ILIKE '%28 day%' THEN 'Monthly'
         WHEN cur.cur_desc ILIKE '%annual%'  THEN 'Annual'
         ELSE NULL
       END AS billing_frequency,
       pay.last_payment_date,
       sub.next_payment_date
FROM base b
LEFT JOIN sub ON sub.hubspot_id = b.hubspot_id
LEFT JOIN inv ON inv.hubspot_id = b.hubspot_id
LEFT JOIN cur ON cur.hubspot_id = b.hubspot_id
LEFT JOIN pay ON pay.hubspot_id = b.hubspot_id
"""


def get_db_connection():
    ctx = ssl.create_default_context()
    return pg8000.connect(
        host=os.environ["DB_HOST"], port=5432, database=os.environ["DB_NAME"],
        user=os.environ["DB_USER"], password=os.environ["DB_PASSWORD"], ssl_context=ctx,
    )


def time_left_ms(context):
    try:
        return context.get_remaining_time_in_millis()
    except Exception:
        return 10 ** 9


# --- Compute + hash ---

def compute_rows(conn):
    cur = conn.cursor()
    cur.execute(BILLING_SQL)
    return [dict(zip(COLUMNS, r)) for r in cur.fetchall()]


def _num(v):
    return "" if v is None else f"{float(v):.2f}"


def _date(v):
    return "" if v is None else v.isoformat()[:10]   # YYYY-MM-DD (date or timestamp)


def to_props(row):
    """The exact HubSpot company property payload for a row (also what we hash)."""
    return {
        "billing_status": row["billing_status"] or "",
        "outstanding_balance": _num(row["outstanding_balance"]),
        "current_billing_amount": _num(row["current_billing_amount"]),
        "billing_frequency": row["billing_frequency"] or "",
        "last_payment_date": _date(row["last_payment_date"]),
        "next_payment_date": _date(row["next_payment_date"]),
    }


def field_hash(props):
    return hashlib.md5(json.dumps(props, sort_keys=True).encode()).hexdigest()


def read_push_state(conn):
    cur = conn.cursor()
    cur.execute("SELECT hubspot_id, fields_hash FROM sf_hubspot_push_state")
    return {r[0]: r[1] for r in cur.fetchall()}


def upsert_push_state(conn, pairs):
    if not pairs:
        return
    cur = conn.cursor()
    cur.executemany(
        "INSERT INTO sf_hubspot_push_state (hubspot_id, fields_hash, pushed_at) "
        "VALUES (%s, %s, now()) "
        "ON CONFLICT (hubspot_id) DO UPDATE SET fields_hash = EXCLUDED.fields_hash, pushed_at = now()",
        pairs,
    )
    conn.commit()


# --- HubSpot write ---

def hs_batch_update(token, inputs):
    return requests.post(
        f"{HUBSPOT_API}/crm/v3/objects/companies/batch/update",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"inputs": inputs},
        timeout=HTTP_TIMEOUT,
    )


def flush_batch(conn, token, batch, errors):
    """POST one batch; mark the succeeded ids in push_state. Returns count written."""
    inputs = [{"id": hid, "properties": props} for hid, _h, props in batch]
    resp = hs_batch_update(token, inputs)
    try:
        data = resp.json() if resp.content else {}
    except ValueError:
        data = {}
    if resp.status_code in (200, 207):
        succeeded = {res.get("id") for res in data.get("results", [])}
        upsert_push_state(conn, [(hid, h) for hid, h, _p in batch if hid in succeeded])
        if data.get("errors"):
            errors.extend(data["errors"][:5])
        return len(succeeded)
    errors.append({"status": resp.status_code, "body": resp.text[:400], "count": len(batch)})
    return 0


# --- Entry point ---

def lambda_handler(event, context):
    conn = get_db_connection()
    summary = {"debug": DEBUG}
    try:
        rows = compute_rows(conn)
        summary["companies_computed"] = len(rows)

        prev = read_push_state(conn)
        # (hubspot_id, hash, props) for companies whose value set changed or is new
        changed = []
        for r in rows:
            props = to_props(r)
            h = field_hash(props)
            if prev.get(r["hubspot_id"]) != h:
                changed.append((r["hubspot_id"], h, props))
        summary["changed"] = len(changed)

        if DEBUG:
            summary["sample_changed"] = [
                {"hubspot_id": hid, **props} for hid, _h, props in changed[:5]
            ]
            summary["sample_computed"] = [
                {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in r.items()}
                for r in rows[:5]
            ]
            return json.loads(json.dumps(summary, default=str))

        token = os.environ["HUBSPOT_TOKEN"]
        pushed, errors, batch = 0, [], []
        for item in changed:
            if time_left_ms(context) < SAFETY_MS:
                summary["stopped_early"] = True
                break
            batch.append(item)
            if len(batch) >= BATCH_SIZE:
                pushed += flush_batch(conn, token, batch, errors)
                batch = []
        if batch and not summary.get("stopped_early"):
            pushed += flush_batch(conn, token, batch, errors)

        summary["pushed"] = pushed
        summary["errors"] = errors[:20]
        print(f"[push] computed={summary['companies_computed']} changed={summary['changed']} "
              f"pushed={pushed} errors={len(errors)}")
        return json.loads(json.dumps(summary, default=str))
    except Exception as e:  # noqa: BLE001
        print(f"[error] {e!r}")
        summary["error"] = str(e)
        return json.loads(json.dumps(summary, default=str))
    finally:
        try:
            conn.close()
        except Exception:
            pass
