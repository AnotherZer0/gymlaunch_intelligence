"""
gymlaunch-subscriptionflow-daily-sync — daily SF -> RDS payment-data sync.

Mirrors SubscriptionFlow ("SF") customers, subscriptions, invoices, transactions,
and products into the `sf_*` tables (migration 015), tracking progress in
`sf_sync_state` (migration 016). Feeds the billing-field push to HubSpot, the churn
signal, and the failed-payment alerting backbone.

METHOD (see docs/future_work.md for the design discussion)
  * Reads GET /<obj>/with-relations (flat fields + customer_id). We do NOT detail-fetch
    per record — with-relations omits the nested items[]/invoices[] arrays, and a detail
    call per record would be an N+1 that can't finish (customers alone are ~116k). All
    billing + churn fields are flat; the nested-derived columns (primary_subscription_id,
    primary_invoice_id, plan_id, billing_frequency) are left NULL for a later enrichment.
  * RESUMABLE backfill: large tables can't be pulled in one 15-min invocation, so the run
    upserts each page immediately, checkpoints `backfill_next_page` in sf_sync_state, and
    stops before the Lambda deadline (SAFETY_MS). The next invocation resumes from the
    checkpoint. Once a full pass completes, the object flips to incremental.
  * Incremental: pulls records changed since `last_watermark` (minus a lookback buffer)
    and UPSERTs by SF `id`. Idempotent, so overlap / re-pulls are harmless.
  * Every row keeps the full SF payload in `raw jsonb` — no field is ever lost, and the
    deferred nested columns can be backfilled from raw or a targeted enrichment pass.

AUTH
  Reuses the OAuth2 client-credentials setup from the subscribe Lambda: token cached in
  `subscriptionflow_oauth_token`, refreshed proactively and rotated on a 401. Needs env
  SF_CLIENT_ID / SF_CLIENT_SECRET (same secret) + the DB_* vars.

ENV FLAGS
  DEBUG=1      Dry run: fetch one bounded page per object, write NOTHING, touch no state,
               and return a small sample + pagination meta + a request trace. Safe.
  FULL_SYNC=1  Force a re-backfill of already-completed objects (set for ONE run, then
               off). Not needed for the first run — empty tables backfill automatically.
"""

import datetime
import json
import os
import ssl

import pg8000
import requests

SF_API_BASE = "https://gymlaunch.subscriptionflow.com/api/v1"
SF_TOKEN_URL = "https://gymlaunch.subscriptionflow.com/oauth/token"
HTTP_TIMEOUT = 30
PAGE_SIZE = 200            # SF's Laravel list default; ?per_page may or may not shrink it
DEBUG_PAGE_SIZE = 5
DEBUG_MAX_RECORDS = 3      # hard client-side cap per object in DEBUG (API may ignore per_page)
LOOKBACK = datetime.timedelta(hours=6)   # re-pull buffer; upsert makes overlap harmless
TOKEN_EXPIRY_BUFFER = 60

DEBUG = os.environ.get("DEBUG", "").strip().lower() in ("1", "true", "yes", "on")
FULL_SYNC = os.environ.get("FULL_SYNC", "").strip().lower() in ("1", "true", "yes", "on")


class SFError(Exception):
    pass


# In DEBUG, every SF call records (method, path, params, status, top-level keys, body
# snippet) here so a dry run shows exactly what SF returned — no guessing at the envelope.
DEBUG_TRACE = []


# --- Connections ---

def get_db_connection():
    ctx = ssl.create_default_context()
    return pg8000.connect(
        host=os.environ["DB_HOST"], port=5432, database=os.environ["DB_NAME"],
        user=os.environ["DB_USER"], password=os.environ["DB_PASSWORD"], ssl_context=ctx,
    )


# --- Token management (mirror of the subscribe Lambda) ---

def _read_cached_token(conn):
    cur = conn.cursor()
    cur.execute("SELECT access_token, expires_at FROM subscriptionflow_oauth_token WHERE id = 1")
    return cur.fetchone()


def rotate_token(conn):
    resp = requests.post(
        SF_TOKEN_URL,
        data={
            "grant_type": "client_credentials",
            "client_id": os.environ["SF_CLIENT_ID"],
            "client_secret": os.environ["SF_CLIENT_SECRET"],
            "scope": "",
        },
        headers={"Accept": "application/json"},
        timeout=HTTP_TIMEOUT,
    )
    if resp.status_code != 200:
        raise SFError(f"token endpoint {resp.status_code}: {resp.text[:300]}")
    payload = resp.json()
    token = payload.get("access_token")
    if not token:
        raise SFError(f"token endpoint returned no access_token: {payload}")
    now = datetime.datetime.now(datetime.timezone.utc)
    expires_in = payload.get("expires_in")
    expires_at = now + datetime.timedelta(seconds=int(expires_in)) if expires_in else None
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO subscriptionflow_oauth_token (id, access_token, token_type, expires_at, obtained_at, updated_at)
        VALUES (1, %s, %s, %s, %s, %s)
        ON CONFLICT (id) DO UPDATE SET
            access_token = EXCLUDED.access_token, token_type = EXCLUDED.token_type,
            expires_at = EXCLUDED.expires_at, updated_at = EXCLUDED.updated_at
        """,
        (token, payload.get("token_type", "Bearer"), expires_at, now, now),
    )
    conn.commit()
    print(f"[token] rotated; expires_at={expires_at}")
    return token


def get_token(conn):
    cached = _read_cached_token(conn)
    if cached is None:
        return rotate_token(conn)
    token, expires_at = cached
    if expires_at is not None:
        now = datetime.datetime.now(datetime.timezone.utc)
        if now >= expires_at - datetime.timedelta(seconds=TOKEN_EXPIRY_BUFFER):
            return rotate_token(conn)
    return token


def sf_request(conn, token_box, method, path, *, params=None):
    url = SF_API_BASE + path

    def _do(tok):
        return requests.request(
            method, url, params=params,
            headers={"Authorization": f"Bearer {tok}", "Accept": "application/json"},
            timeout=HTTP_TIMEOUT,
        )

    resp = _do(token_box[0])
    if resp.status_code == 401:
        print(f"[sf] 401 on {method} {path}; rotating + retrying")
        token_box[0] = rotate_token(conn)
        resp = _do(token_box[0])
    if DEBUG:
        try:
            keys = list((resp.json() or {}).keys())
        except ValueError:
            keys = None
        DEBUG_TRACE.append({
            "method": method, "path": path, "params": params,
            "status": resp.status_code, "top_level_keys": keys,
            "body_snippet": resp.text[:600],
        })
    if resp.status_code not in (200, 201):
        raise SFError(f"{method} {path} -> {resp.status_code}: {resp.text[:300]}")
    return resp.json() or {}


# --- Coercion helpers (SF is loose with types: 0/1, "Yes"/"No", numeric strings, "N/A") ---

def to_bool(v):
    if v in (1, "1", True, "Yes", "yes", "true", "True"):
        return True
    if v in (0, "0", False, "No", "no", "false", "False"):
        return False
    return None


def to_num(v):
    if v is None or v == "" or v == "N/A":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def to_dt(v):
    if not v:
        return None
    try:
        return datetime.datetime.fromisoformat(str(v))
    except ValueError:
        return None


def to_date(v):
    if not v:
        return None
    try:
        return datetime.date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def derive_frequency(plan_price):
    """Best-effort Weekly/Monthly/Quarterly/PIF from the embedded plan_price. Raw kept as backstop."""
    if not isinstance(plan_price, dict):
        return None
    if (plan_price.get("category") or "").lower() == "onetime":
        return "PIF"
    weeks = plan_price.get("billing_period_months_weeks")
    mapping = {1: "Weekly", 2: "Biweekly", 4: "Monthly", 13: "Quarterly", 26: "Semiannual", 52: "Annual"}
    try:
        return mapping.get(int(weeks))
    except (TypeError, ValueError):
        return None


def normalize(record):
    """List/filter records are flat; detail records wrap fields under `attributes`. Unify."""
    if isinstance(record, dict) and isinstance(record.get("attributes"), dict):
        return record["attributes"]
    return record


# --- Per-object field mappers (attrs -> row dict; raw always carries the full payload) ---

def map_customer(a):
    return {
        "id": a.get("id"), "hubspot_id": a.get("hubspot_id"),
        "accounting_resource_id": a.get("accounting_resource_id"),
        "name": a.get("name"), "email": a.get("email"), "phone_number": a.get("phone_number"),
        "currency": a.get("currency"), "auto_charge": to_bool(a.get("auto_charge")),
        "data_source": a.get("data_source"),
        "primary_churn_score_value": to_num(a.get("primary_churn_score_value")),
        "primary_churn_score_grade": a.get("primary_churn_score_grade"),
        "parent_id": a.get("parent_id"),
        "created_at": to_dt(a.get("created_at")), "updated_at": to_dt(a.get("updated_at")),
        "raw": a,
    }


def _first_item_bits(a):
    items = a.get("items") or []
    item0 = items[0] if items else {}
    charges = item0.get("charges") or []
    charge0 = charges[0] if charges else {}
    plan_price = charge0.get("plan_price") or {}
    plan_price_id = charge0.get("plan_price_id") or plan_price.get("id")
    return item0, plan_price, plan_price_id


def map_subscription(a):
    item0, plan_price, plan_price_id = _first_item_bits(a)
    additional = a.get("additional_data") if isinstance(a.get("additional_data"), dict) else {}
    return {
        "id": a.get("id"), "name": a.get("name"), "display_name": a.get("display_name"),
        "hubspot_id": a.get("hubspot_id"), "hubspot_deal_id": additional.get("hubspot_deal_id"),
        "customer_id": (a.get("customer") or {}).get("id") or a.get("customer_id"),
        "status": a.get("status"), "payment_status": a.get("payment_status"),
        "type": a.get("type"), "termed_start_date": to_date(a.get("termed_start_date")),
        "termed_initial_period": a.get("termed_initial_period"),
        "termed_initial_period_type": a.get("termed_initial_period_type"),
        "renewal_type": a.get("renewal_type"), "renewal_period": a.get("renewal_period"),
        "renewal_period_type": a.get("renewal_period_type"),
        "is_auto_renew": to_bool(a.get("is_auto_renew")),
        "billing_end_date": to_date(a.get("billing_end_date")),
        "next_bill_date": to_date(a.get("next_bill_date")),
        "billing_frequency": derive_frequency(plan_price),
        "total_amount": to_num(a.get("total_amount")),
        "plan_id": item0.get("plan_id"), "product_id": item0.get("product_id"),
        "plan_price_id": plan_price_id,
        "suspended_at": to_dt(a.get("suspended_at")), "cancelled_at": to_dt(a.get("cancelled_at")),
        "renewed_at": to_dt(a.get("renewed_at")), "mv_remaining_term": a.get("mv_remaining_term"),
        "data_source": a.get("data_source"),
        "created_at": to_dt(a.get("created_at")), "updated_at": to_dt(a.get("updated_at")),
        "raw": a,
    }


def map_invoice(a):
    items = a.get("items") or []
    return {
        "id": a.get("id"), "name": a.get("name"), "hubspot_id": a.get("hubspot_id"),
        "customer_id": (a.get("customer") or {}).get("id") or a.get("customer_id"),
        "primary_subscription_id": items[0].get("subscription_id") if items else None,
        "invoice_date": to_date(a.get("invoice_date")), "due_date": to_date(a.get("due_date")),
        "status": a.get("status"), "sub_total": to_num(a.get("sub_total")),
        "total_amount": to_num(a.get("total_amount")),
        "received_payment": to_num(a.get("received_payment")),
        "opening_balance": to_num(a.get("opening_balance")),
        "closing_balance": to_num(a.get("closing_balance")),
        "sum_of_credit_notes": to_num(a.get("sum_of_credit_notes")),
        "tax_amount": to_num(a.get("tax_amount")), "discount_value": to_num(a.get("discount_value")),
        "currency": a.get("currency"), "is_oneoff": to_bool(a.get("is_oneoff")),
        "description": a.get("description"), "note": a.get("note"),
        "data_source": a.get("data_source"),
        "created_at": to_dt(a.get("created_at")), "updated_at": to_dt(a.get("updated_at")),
        "raw": a,
    }


def map_transaction(a):
    invoices = a.get("invoices") or []
    return {
        "id": a.get("id"), "name": a.get("name"), "number": a.get("number"),
        "gateway_transaction_id": a.get("transaction_id"),
        "customer_id": (a.get("customer") or {}).get("id") or a.get("customer_id"),
        "primary_invoice_id": invoices[0].get("invoice_id") if invoices else None,
        "date": to_dt(a.get("date")), "status": a.get("status"),
        "amount": to_num(a.get("amount")), "balance": to_num(a.get("balance")),
        "unapplied_amount": to_num(a.get("unapplied_amount")),
        "type": a.get("type"), "transaction_category": a.get("transaction_category"),
        "cash_or_card": a.get("cash_or_card"), "payment_type_id": a.get("payment_type_id"),
        "payment_method_id": a.get("payment_method_id"), "decline_reason": a.get("decline_reason"),
        "reason_code": a.get("reason_code"),
        "reference_transaction_id": a.get("reference_transaction_id"),
        "description": a.get("description"), "currency": a.get("currency"),
        "data_source": a.get("data_source"),
        "created_at": to_dt(a.get("created_at")), "updated_at": to_dt(a.get("updated_at")),
        "raw": a,
    }


def map_product(a):
    return {
        "id": a.get("id"), "name": a.get("name"), "description": a.get("description"),
        "sku": a.get("sku") or a.get("product_sku"), "position": a.get("position") or a.get("product_position"),
        "image": a.get("image") or a.get("product_image"), "data_source": a.get("data_source"),
        "created_at": to_dt(a.get("created_at")), "updated_at": to_dt(a.get("updated_at")),
        "raw": a,
    }


# Order matters: smallest / most billing-and-churn-relevant first, the ~116k-record
# customer table LAST — so the first run backfills products/subs/invoices/transactions
# fully (a few minutes) before spending the rest of its time chipping at customers,
# instead of customers starving everything else for days.
OBJECTS = [
    {"key": "product", "path": "products", "table": "sf_product", "map": map_product},
    {"key": "subscription", "path": "subscriptions", "table": "sf_subscription", "map": map_subscription},
    {"key": "invoice", "path": "invoices", "table": "sf_invoice", "map": map_invoice},
    {"key": "transaction", "path": "transactions", "table": "sf_transaction", "map": map_transaction},
    {"key": "customer", "path": "customers", "table": "sf_customer", "map": map_customer},
]


# --- Fetch + upsert ---
#
# NOTE: we do NOT detail-fetch per record. GET /<obj>/with-relations gives flat fields
# + customer_id but NOT the nested items[]/invoices[] arrays, so a detail call per record
# would be an N+1 explosion (116k customers, 13k transactions) that can't finish in
# Lambda's 15 min. Everything billing + churn need is in the flat record; the nested-
# derived fields (primary_subscription_id / primary_invoice_id / plan_id / billing_frequency)
# are left NULL for now and can be backfilled from a targeted enrichment pass later.

SAFETY_MS = 60_000   # stop + checkpoint when under this much wall-clock remains


def table_watermark(conn, table):
    cur = conn.cursor()
    cur.execute(f"SELECT max(updated_at) FROM {table}")
    return cur.fetchone()[0]


def read_state(conn, obj_type):
    cur = conn.cursor()
    cur.execute(
        "SELECT backfill_done, backfill_next_page, last_watermark FROM sf_sync_state WHERE object_type = %s",
        (obj_type,),
    )
    row = cur.fetchone()
    if row:
        return {"backfill_done": row[0], "next_page": row[1], "last_watermark": row[2]}
    cur.execute("INSERT INTO sf_sync_state (object_type) VALUES (%s) ON CONFLICT DO NOTHING", (obj_type,))
    conn.commit()
    return {"backfill_done": False, "next_page": 1, "last_watermark": None}


def write_state(conn, obj_type, **fields):
    col_of = {"backfill_done": "backfill_done", "next_page": "backfill_next_page",
              "last_watermark": "last_watermark"}
    sets, vals = [], []
    for k, col in col_of.items():
        if k in fields:
            sets.append(f"{col} = %s")
            vals.append(fields[k])
    if not sets:
        return
    sets.append("updated_at = now()")
    vals.append(obj_type)
    cur = conn.cursor()
    cur.execute(f"UPDATE sf_sync_state SET {', '.join(sets)} WHERE object_type = %s", vals)
    conn.commit()


def time_left_ms(context):
    try:
        return context.get_remaining_time_in_millis()
    except Exception:
        return 10 ** 9   # no Lambda context (e.g. local run) -> treat as unlimited


def upsert(conn, table, rows):
    if not rows:
        return 0
    cols = list(rows[0].keys())
    placeholders = ", ".join("%s::jsonb" if c == "raw" else "%s" for c in cols)
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c != "id")
    sql = (
        f"INSERT INTO {table} ({', '.join(cols)}, synced_at) "
        f"VALUES ({placeholders}, now()) "
        f"ON CONFLICT (id) DO UPDATE SET {updates}, synced_at = now()"
    )
    cur = conn.cursor()
    for r in rows:
        vals = [json.dumps(r[c], default=str) if c == "raw" else r[c] for c in cols]
        cur.execute(sql, vals)
    conn.commit()
    return len(rows)


def fetch_page(conn, token_box, obj, page, per_page):
    """One BACKFILL page of GET /<obj>/with-relations -> (mapped rows, meta, links, count)."""
    payload = sf_request(conn, token_box, "GET", f"/{obj['path']}/with-relations",
                         params={"page": page, "per_page": per_page})
    records = payload.get("data") or []
    rows = [obj["map"](normalize(r)) for r in records]
    return rows, (payload.get("meta") or {}), (payload.get("links") or {}), len(records)


def fetch_incremental_page(conn, token_box, obj, offset, per_page, watermark):
    """
    One INCREMENTAL page via POST /<obj>/filter with an updated_at condition.

    The plain list / with-relations endpoints IGNORE filter[updated_at] (they re-return
    everything), so incremental uses /filter — SF's conditional-query endpoint, which is
    built to honor filter[column][$gte] and paginates with filter[$limit]/[$offset].
    """
    params = {
        "filter[$limit]": per_page,
        "filter[$offset]": offset,
        "filter[updated_at][$gte]": (watermark - LOOKBACK).isoformat(),
    }
    payload = sf_request(conn, token_box, "POST", f"/{obj['path']}/filter", params=params)
    records = payload.get("data") or []
    rows = [obj["map"](normalize(r)) for r in records]
    return rows, len(records)


def sample_object(conn, token_box, obj, stats):
    """DEBUG dry run: fetch one bounded page, write nothing, touch no state."""
    rows, meta, _links, page_count = fetch_page(conn, token_box, obj, 1, DEBUG_PAGE_SIZE)
    rows = rows[:DEBUG_MAX_RECORDS]
    stats["fetched"] = len(rows)
    stats["pagination"] = {
        "current_page": meta.get("current_page"), "last_page": meta.get("last_page"),
        "per_page": meta.get("per_page"), "first_page_count": page_count,
    }
    stats["sample"] = [{k: v for k, v in r.items() if k != "raw"} for r in rows]


def _last_page(page, meta, links, page_count):
    lp = meta.get("last_page") or page
    return page_count == 0 or page >= lp or not links.get("next")


def process_object(conn, token_box, obj, context, stats):
    """
    Live sync. Resumable paginated backfill (checkpoints backfill_next_page and stops
    before the Lambda deadline), then incremental-by-watermark once backfilled. Every
    page is upserted immediately, so progress is durable if the run is cut short.
    """
    st = read_state(conn, obj["key"])

    if not st["backfill_done"]:
        stats["mode"] = "backfill"
        page = st["next_page"]
        while True:
            if time_left_ms(context) < SAFETY_MS:
                stats["stopped_early"] = True
                write_state(conn, obj["key"], next_page=page)
                return
            rows, meta, links, page_count = fetch_page(conn, token_box, obj, page, PAGE_SIZE)
            stats["upserted"] += upsert(conn, obj["table"], rows)
            stats["pages"] = stats.get("pages", 0) + 1
            stats["last_page"] = meta.get("last_page")
            if _last_page(page, meta, links, page_count):
                write_state(conn, obj["key"], backfill_done=True,
                            last_watermark=table_watermark(conn, obj["table"]))
                stats["backfill_done"] = True
                return
            page += 1
            write_state(conn, obj["key"], next_page=page)
        return

    stats["mode"] = "incremental"
    wm = st["last_watermark"] or table_watermark(conn, obj["table"])
    if wm is None:
        return   # nothing synced yet, nothing to filter from
    offset = 0
    while True:
        if time_left_ms(context) < SAFETY_MS:
            stats["stopped_early"] = True
            break
        rows, count = fetch_incremental_page(conn, token_box, obj, offset, PAGE_SIZE, wm)
        stats["upserted"] += upsert(conn, obj["table"], rows)
        stats["pages"] = stats.get("pages", 0) + 1
        if count < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    write_state(conn, obj["key"], last_watermark=(table_watermark(conn, obj["table"]) or wm))


def lambda_handler(event, context):
    conn = get_db_connection()
    summary = {"debug": DEBUG, "objects": {}}
    try:
        token_box = [get_token(conn)]
        if FULL_SYNC and not DEBUG:
            # Force a re-backfill of already-completed objects. Only resets rows where
            # backfill_done=true, so leaving FULL_SYNC=1 set can't re-reset an in-progress
            # backfill each invocation (it would never finish). Set it for one run, then off.
            cur = conn.cursor()
            cur.execute("UPDATE sf_sync_state SET backfill_done = false, backfill_next_page = 1 "
                        "WHERE backfill_done = true")
            conn.commit()
            summary["full_sync_reset"] = True
        for obj in OBJECTS:
            stats = {"upserted": 0}
            if DEBUG:
                sample_object(conn, token_box, obj, stats)
            else:
                process_object(conn, token_box, obj, context, stats)
            summary["objects"][obj["key"]] = stats
            print(f"[{obj['key']}] {stats}")
        if DEBUG:
            summary["trace"] = DEBUG_TRACE
        # Coerce to JSON-safe (samples carry datetime/date objects Lambda can't marshal).
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
