"""
gymlaunch-sf-create-custom-weekly-sub-for-go-product — Function URL endpoint

Given a customer (by SubscriptionFlow id, falling back to email), find or create
that customer in SubscriptionFlow ("SF"), then create a one-year *Termed*
subscription for them. "Termed" = a fixed-term subscription that ENDS after its
term (vs "Evergreen", which renews forever), so a 1-year term bills until the
year is up and then stops. The weekly billing cadence is NOT set here — it comes
from the SF plan/price config, so the default plan must be a weekly plan.

The first invoice is left DUE (pay_invoice is not enabled) so SF does not
auto-charge or fire successful-payment logic.

SF authenticates with an OAuth2 client-credentials grant. The client_id /
client_secret live in env vars; the short-lived bearer access_token is cached in
the `subscriptionflow_oauth_token` table (singleton). This handler rotates the
token in-band: it refreshes proactively when the cached token is expired/missing,
and reactively on a 401 from SF (then retries the call once).

AUTH (caller -> this endpoint)
  The request must present the shared secret, matching env SF_ENDPOINT_API_KEY,
  as EITHER the `x-api-key` header OR a `key` query-string parameter.

INPUT  (JSON body or query string)
  id              SF customer id        (optional — tried first if present)
  email           customer email        (required if id missing or not found;
                                          also used as the name when creating)
  price           per-item charge price (optional — defaults to 0.00)
  product_id      SF product id         (optional — default below)
  plan_id         SF plan id            (optional — default below)
  plan_price_id   SF charge id          (optional — default below; this is the
                                          key of the `charges` object)
  start_date      YYYY-MM-DD            (optional — defaults to today UTC; used
                                          for order_date, trigger_dates and the
                                          termed_start_date)

RESPONSE
  JSON. On success: {"ok": true, "sf_customer_id": ..., "sf_subscription_id": ...,
  "customer_created": bool}. On failure: HTTP 4xx/5xx with {"ok": false,
  "error": "..."}.

DEBUG / DRY RUN
  Set env var DEBUG=1 to do a safe dry run: authenticate, fetch a token, look up
  the customer read-only, then return the exact subscription body that WOULD be
  posted — without creating the customer or the subscription. Use it to validate
  the first webhook fire before sending live traffic.
"""

import base64
import datetime
import json
import os
import ssl

import pg8000
import requests

# --- SubscriptionFlow constants ---

SF_API_BASE = "https://gymlaunch.subscriptionflow.com/api/v1"
SF_TOKEN_URL = "https://gymlaunch.subscriptionflow.com/oauth/token"
HTTP_TIMEOUT = 25  # seconds; stays under the typical 29s gateway/URL ceiling

# Defaults for the GO product subscription when the caller omits them.
DEFAULT_PRODUCT_ID = "fe483af0-2b49-4a5c-8a4b-9fe9caccd067"
DEFAULT_PLAN_ID = "f359c92d-c0d7-4594-961a-f46158cb459f"
DEFAULT_PLAN_PRICE_ID = "f73a8805-ae97-457a-9284-b14cb353dd45"
DEFAULT_PRICE = 0.0  # placeholder; per agreement we correct the amount after the fact

# Term is always one year.
TERMED_INITIAL_PERIOD = 1
TERMED_INITIAL_PERIOD_TYPE = "year"

# Renewal config. SF *requires* renewal_type for Termed subscriptions (Evergreen
# doesn't need it) — omitting it returns 422 "The renewal type field is required."
# We want the subscription to END after its one-year term, so IS_AUTO_RENEW = 0 is
# the lever that actually stops it rolling over; renewal_type just has to be a
# valid present value. "Renew with Specific Term" is the value confirmed by the SF
# OpenAPI spec. If SF later wants a literal "do not renew" value, change RENEWAL_TYPE.
RENEWAL_TYPE = "Renew with Specific Term"
RENEWAL_PERIOD = 1
RENEWAL_PERIOD_TYPE = "year"
IS_AUTO_RENEW = 0  # 0 = does not auto-renew (subscription ends after the term)

# Refresh the cached token this many seconds *before* its stated expiry so we
# don't race the clock on a long-running call.
TOKEN_EXPIRY_BUFFER = 60

# Debug switch — set env var DEBUG=1 (in the console) to make the handler do a
# SAFE DRY RUN: it authenticates, fetches a token, and looks up the customer
# (read-only), then RETURNS the exact subscription body it *would* POST — without
# creating the customer or the subscription. Turn it OFF (remove or set 0) for
# real traffic. Accepts 1/true/yes/on (any case).
DEBUG = os.environ.get("DEBUG", "").strip().lower() in ("1", "true", "yes", "on")


class SFError(Exception):
    """Raised for unrecoverable SubscriptionFlow / input errors. Carries an HTTP status."""

    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


# --- Connections ---

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


# --- Token management (OAuth2 client-credentials) ---

def _read_cached_token(conn):
    """Return (access_token, token_type, expires_at) from the singleton row, or None."""
    cur = conn.cursor()
    cur.execute(
        "SELECT access_token, token_type, expires_at "
        "FROM subscriptionflow_oauth_token WHERE id = 1"
    )
    row = cur.fetchone()
    return row if row else None


def _fetch_new_token():
    """Exchange client credentials for a fresh access token. Returns the SF JSON dict."""
    client_id = os.environ["SF_CLIENT_ID"]
    client_secret = os.environ["SF_CLIENT_SECRET"]
    resp = requests.post(
        SF_TOKEN_URL,
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": "",
        },
        headers={"Accept": "application/json"},
        timeout=HTTP_TIMEOUT,
    )
    if resp.status_code != 200:
        raise SFError(
            f"token endpoint returned {resp.status_code}: {resp.text[:300]}",
            status=502,
        )
    payload = resp.json()
    if not payload.get("access_token"):
        raise SFError(f"token endpoint returned no access_token: {payload}", status=502)
    return payload


def rotate_token(conn):
    """Fetch a new token, persist it to the singleton row, and return the access_token."""
    payload = _fetch_new_token()
    access_token = payload["access_token"]
    token_type = payload.get("token_type", "Bearer")
    expires_in = payload.get("expires_in")
    now = datetime.datetime.now(datetime.timezone.utc)
    expires_at = (
        now + datetime.timedelta(seconds=int(expires_in)) if expires_in else None
    )

    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO subscriptionflow_oauth_token
            (id, access_token, token_type, expires_at, obtained_at, updated_at)
        VALUES (1, %s, %s, %s, %s, %s)
        ON CONFLICT (id) DO UPDATE SET
            access_token = EXCLUDED.access_token,
            token_type   = EXCLUDED.token_type,
            expires_at   = EXCLUDED.expires_at,
            updated_at   = EXCLUDED.updated_at
        """,
        (access_token, token_type, expires_at, now, now),
    )
    conn.commit()
    print(f"[token] rotated; expires_at={expires_at}")
    return access_token


def get_token(conn):
    """Return a usable access token, refreshing proactively if missing/expired."""
    cached = _read_cached_token(conn)
    if cached is None:
        print("[token] no cached token; fetching")
        return rotate_token(conn)

    access_token, _token_type, expires_at = cached
    if expires_at is not None:
        now = datetime.datetime.now(datetime.timezone.utc)
        if now >= expires_at - datetime.timedelta(seconds=TOKEN_EXPIRY_BUFFER):
            print("[token] cached token expired/near-expiry; refreshing")
            return rotate_token(conn)
    return access_token


# --- SF request wrapper with reactive 401 rotation ---

def sf_request(conn, token_box, method, path, *, params=None, json_body=None):
    """
    Call SF at SF_API_BASE + path. On a 401, rotate the token once and retry.

    token_box is a single-element list holding the current access token so the
    rotated value propagates back to the caller for subsequent calls.
    """
    url = SF_API_BASE + path

    def _do(tok):
        return requests.request(
            method,
            url,
            params=params,
            json=json_body,
            headers={
                "Authorization": f"Bearer {tok}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=HTTP_TIMEOUT,
        )

    resp = _do(token_box[0])
    if resp.status_code == 401:
        print(f"[sf] 401 on {method} {path}; rotating token and retrying once")
        token_box[0] = rotate_token(conn)
        resp = _do(token_box[0])
    return resp


# --- Customer find-or-create ---

def find_customer_by_id(conn, token_box, sf_id):
    """GET /customers/{id}. Returns the SF customer id string, or None if not found."""
    resp = sf_request(conn, token_box, "GET", f"/customers/{sf_id}")
    if resp.status_code == 200:
        data = (resp.json() or {}).get("data") or {}
        # A 200 with no data.id means "not found" — fall through to email lookup.
        return data.get("id")
    if resp.status_code in (400, 404):
        return None
    raise SFError(
        f"lookup by id failed ({resp.status_code}): {resp.text[:300]}", status=502
    )


def find_customer_by_email(conn, token_box, email):
    """POST /customers/filter with filter[email][$equals]. Returns SF id or None."""
    resp = sf_request(
        conn,
        token_box,
        "POST",
        "/customers/filter",
        params={"filter[email][$equals]": email},
    )
    if resp.status_code in (200, 201):
        rows = (resp.json() or {}).get("data") or []
        if rows:
            return rows[0].get("id")
        return None
    raise SFError(
        f"lookup by email failed ({resp.status_code}): {resp.text[:300]}", status=502
    )


def create_customer(conn, token_box, email):
    """POST /customers using the email as both name and email. Returns the new SF id."""
    resp = sf_request(
        conn,
        token_box,
        "POST",
        "/customers",
        json_body={"name": email, "email": email},
    )
    if resp.status_code in (200, 201):
        data = (resp.json() or {}).get("data") or {}
        new_id = data.get("id")
        if not new_id:
            raise SFError(f"customer create returned no id: {resp.text[:300]}", status=502)
        return new_id
    raise SFError(
        f"customer create failed ({resp.status_code}): {resp.text[:300]}", status=502
    )


def resolve_customer(conn, token_box, sf_id, email):
    """
    Find the customer by id, then by email, else create one.
    Returns (customer_id, created_bool).
    """
    if sf_id:
        found = find_customer_by_id(conn, token_box, sf_id)
        if found:
            return found, False

    if not email:
        raise SFError(
            "no customer found by id and no email provided to search/create", status=400
        )

    found = find_customer_by_email(conn, token_box, email)
    if found:
        return found, False

    return create_customer(conn, token_box, email), True


# --- Subscription create ---

def build_subscription_body(customer_id, *, product_id, plan_id,
                            plan_price_id, price, the_date):
    """Assemble the POST /subscriptions JSON body for a one-year Termed sub."""
    return {
        "customer_id": customer_id,
        "type": "Termed",
        "order_date": the_date,
        "trigger_dates": the_date,
        "termed_start_date": the_date,
        "termed_initial_period": TERMED_INITIAL_PERIOD,
        "termed_initial_period_type": TERMED_INITIAL_PERIOD_TYPE,
        # Required by SF for Termed subs. is_auto_renew=0 makes it end after the term.
        "renewal_type": RENEWAL_TYPE,
        "renewal_period": RENEWAL_PERIOD,
        "renewal_period_type": RENEWAL_PERIOD_TYPE,
        "is_auto_renew": IS_AUTO_RENEW,
        "items": [
            {
                "plan_id": plan_id,
                "product_id": product_id,
                "charges": {
                    plan_price_id: {
                        "quantity": 1,
                        "price": price,
                    }
                },
            }
        ],
        # Leave the generated invoice DUE — do NOT auto-charge. Enabling this
        # would fire SF's successful-payment logic, which we explicitly don't want.
        "pay_invoice": False,
    }


def create_subscription(conn, token_box, customer_id, *, product_id, plan_id,
                        plan_price_id, price, the_date):
    """POST /subscriptions for a one-year Termed sub. Returns the SF subscription id."""
    body = build_subscription_body(
        customer_id, product_id=product_id, plan_id=plan_id,
        plan_price_id=plan_price_id, price=price, the_date=the_date,
    )
    resp = sf_request(conn, token_box, "POST", "/subscriptions", json_body=body)
    if resp.status_code in (200, 201):
        payload = resp.json() or {}
        # Subscription create may return the resource at the top level or under `data`.
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        sub_id = data.get("id")
        if not sub_id:
            raise SFError(
                f"subscription create returned no id: {resp.text[:300]}", status=502
            )
        return sub_id
    raise SFError(
        f"subscription create failed ({resp.status_code}): {resp.text[:400]}", status=502
    )


# --- Request / response helpers ---

def reply(status, payload):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload),
    }


def parse_event(event):
    """Return (api_key, params_dict) from a Lambda Function URL event."""
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    qs = event.get("queryStringParameters") or {}
    api_key = headers.get("x-api-key") or qs.get("key")

    params = dict(qs)
    raw = event.get("body") or ""
    if raw:
        if event.get("isBase64Encoded"):
            raw = base64.b64decode(raw).decode("utf-8", "replace")
        try:
            body = json.loads(raw)
            if isinstance(body, dict):
                params.update(body)  # JSON body wins over query string
        except (ValueError, TypeError):
            pass
    return api_key, params


def resolve_date(raw):
    """Validate a YYYY-MM-DD string; default to today (UTC) if absent. Raises on malformed."""
    if not raw:
        return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    raw = str(raw).strip()
    try:
        datetime.datetime.strptime(raw, "%Y-%m-%d")
    except ValueError:
        raise SFError(f"start_date must be YYYY-MM-DD, got: {raw!r}", status=400)
    return raw


def resolve_price(raw):
    """Coerce the incoming price to a float; default to DEFAULT_PRICE if absent."""
    if raw is None or str(raw).strip() == "":
        print(f"[input] no price provided; defaulting to {DEFAULT_PRICE}")
        return DEFAULT_PRICE
    try:
        return float(raw)
    except (ValueError, TypeError):
        raise SFError(f"price must be numeric, got: {raw!r}", status=400)


# --- Debug dry run ---

def debug_dry_run(conn, token_box, sf_id, email, *, product_id, plan_id,
                  plan_price_id, price, the_date, raw_params):
    """
    Safe dry run for DEBUG mode. Authenticates, fetches a token, and looks up the
    customer READ-ONLY, then returns the exact subscription body that *would* be
    POSTed. Creates nothing in SubscriptionFlow. Never returns secret values.
    """
    token = token_box[0]

    resolved_id = None
    via = None
    would_create = False
    if sf_id:
        resolved_id = find_customer_by_id(conn, token_box, sf_id)
        if resolved_id:
            via = "id"
    if not resolved_id and email:
        resolved_id = find_customer_by_email(conn, token_box, email)
        if resolved_id:
            via = "email"
    if not resolved_id:
        would_create = True
        via = "would_create"

    customer_id_for_body = resolved_id or "<NEW_CUSTOMER_ID>"
    body = build_subscription_body(
        customer_id_for_body, product_id=product_id, plan_id=plan_id,
        plan_price_id=plan_price_id, price=price, the_date=the_date,
    )

    return reply(200, {
        "ok": True,
        "debug": True,
        "note": "DRY RUN — no customer or subscription was created in SubscriptionFlow.",
        "received": {
            "id": sf_id,
            "email": email,
            "raw_params": raw_params,
        },
        "resolved": {
            "product_id": product_id,
            "plan_id": plan_id,
            "plan_price_id": plan_price_id,
            "price": price,
            "date": the_date,
        },
        "token": {"acquired": bool(token), "length": len(token) if token else 0},
        "customer": {
            "resolved_customer_id": resolved_id,
            "via": via,
            "would_create": would_create,
            "would_create_with": (
                {"name": email, "email": email} if would_create else None
            ),
        },
        "would_post_subscription_body": body,
    })


# --- Entry point ---

def lambda_handler(event, context):
    expected_key = os.environ.get("SF_ENDPOINT_API_KEY")
    api_key, params = parse_event(event)
    if not expected_key or api_key != expected_key:
        return reply(401, {"ok": False, "error": "unauthorized"})

    conn = None
    try:
        sf_id = (params.get("id") or "").strip() or None
        email = (params.get("email") or "").strip() or None
        the_date = resolve_date(params.get("start_date"))
        price = resolve_price(params.get("price"))
        product_id = (params.get("product_id") or "").strip() or DEFAULT_PRODUCT_ID
        plan_id = (params.get("plan_id") or "").strip() or DEFAULT_PLAN_ID
        plan_price_id = (params.get("plan_price_id") or "").strip() or DEFAULT_PLAN_PRICE_ID

        if not sf_id and not email:
            return reply(400, {"ok": False, "error": "provide at least one of: id, email"})

        conn = get_db_connection()
        token_box = [get_token(conn)]

        if DEBUG:
            return debug_dry_run(
                conn, token_box, sf_id, email,
                product_id=product_id, plan_id=plan_id,
                plan_price_id=plan_price_id, price=price, the_date=the_date,
                raw_params=params,
            )

        customer_id, created = resolve_customer(conn, token_box, sf_id, email)
        sub_id = create_subscription(
            conn, token_box, customer_id,
            product_id=product_id, plan_id=plan_id,
            plan_price_id=plan_price_id, price=price, the_date=the_date,
        )

        return reply(200, {
            "ok": True,
            "sf_customer_id": customer_id,
            "sf_subscription_id": sub_id,
            "customer_created": created,
        })
    except SFError as e:
        print(f"[error] {e}")
        return reply(e.status, {"ok": False, "error": str(e)})
    except Exception as e:  # noqa: BLE001 — surface anything else as a 500
        print(f"[error] unexpected: {e!r}")
        return reply(500, {"ok": False, "error": f"unexpected: {e}"})
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
