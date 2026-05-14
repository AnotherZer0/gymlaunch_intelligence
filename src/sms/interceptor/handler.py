"""
SMS Interceptor Lambda
Sits in front of Octopods for all Twilio webhook traffic.

POST /sms/inbound  — inbound messages from any of our Twilio numbers
POST /sms/status   — delivery status callbacks (failed, undelivered, etc.)

For each inbound message:
  1. Validate the Twilio HMAC-SHA1 signature (reject with 403 on failure)
  2. Identify which channel the message arrived on (Marketing / Product Updates)
     using the TWILIO_NUMBER_CHANNELS env var mapping
  3. If the body is a CTIA opt-out keyword (STOP / UNSUBSCRIBE / etc.):
       - Find the HubSpot contact by phone number
       - Remove the channel from sms_subscriptions
       - Set the per-channel sms_*_opted_out = true
       - Set sms_marketing_opted_out_date (Marketing channel only)
       - Create a timeline note on the contact
  4. If the body is a CTIA opt-in keyword (START / UNSTOP / YES):
       - Find the contact
       - Add the channel back to sms_subscriptions
       - Set the per-channel sms_*_opted_out = false
       - Create a timeline note
  5. Write a row to sms_inbound_message
  6. Forward the raw request to Octopods
  7. Return empty TwiML <Response/> (Twilio handles STOP auto-reply itself)

For each status callback:
  1. Validate signature
  2. If error_code is a hard failure (30006/30007/30008):
       - Find the HubSpot contact by the recipient number
       - Set sms_deliverable = false, sms_ineligible_reason = reason code
  3. Write a row to sms_delivery_event
  4. Forward to Octopods
  5. Return 200

Always returns 200 (or valid TwiML) to Twilio — Twilio retries on non-200,
which would cause duplicate DB rows and duplicate HubSpot updates.

--- HubSpot properties this Lambda writes ---
  sms_subscriptions           multiple checkbox  enrollment state per channel
  sms_marketing_opted_out     checkbox           set on STOP/cleared on START for Marketing
  sms_product_updates_opted_out checkbox         set on STOP/cleared on START for Product Updates
  sms_marketing_opted_out_date date              timestamp of most recent Marketing opt-out
  sms_deliverable             checkbox           false on hard delivery failure
  sms_ineligible_reason       single-line text   reason code for delivery failure

All five properties must be created in HubSpot portal 43776308 before this Lambda
will write to them. A 400 from the HubSpot API means a property doesn't exist yet.

--- Channel mapping ---
Configured via TWILIO_NUMBER_CHANNELS env var (JSON, set in template.yaml).
The Lambda inverts it to a number→channel dict at module load time.
See how_to_update.txt for instructions on adding new numbers.
"""

import base64
import hashlib
import hmac as _hmac
import json
import os
import ssl
import time
import urllib.parse

import pg8000
import requests


# ---------------------------------------------------------------------------
# Opt-out / opt-in keywords (CTIA spec)
# ---------------------------------------------------------------------------

OPT_OUT_KEYWORDS = frozenset(["STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT", "OPTOUT", "REVOKE"])
OPT_IN_KEYWORDS  = frozenset(["START", "UNSTOP", "YES"])

# Twilio error codes that signal a number is permanently undeliverable.
# 30003 (device unreachable) is intentionally excluded — that's transient.
HARD_FAILURE_CODES = {
    "30006": "geo_block",          # Landline or carrier marks number unreachable
    "30007": "carrier_violation",  # Carrier blocked for spam / content policy
    "30008": "carrier_error",      # Unknown carrier-side failure
}


HUBSPOT_BASE    = "https://api.hubapi.com"
REQUEST_TIMEOUT = 8  # seconds — keeps total Lambda wall time well under the 29s API GW limit


# ---------------------------------------------------------------------------
# Channel configuration
#
# Maps channel name → the HubSpot property names to write when a contact
# opts out or in on that channel.
#
# To add a new channel:
#   1. Add an entry here with the property names you've created in HubSpot
#   2. Add the phone number(s) to TWILIO_NUMBER_CHANNELS in template.yaml
#   See how_to_update.txt for the full walkthrough.
# ---------------------------------------------------------------------------

_CHANNEL_PROPS = {
    "Marketing": {
        "opted_out":      "sms_marketing_opted_out",
        "opted_out_date": "sms_marketing_opted_out_date",  # Date property; set on STOP, not cleared on START
        "subscription_value": "Marketing",                  # Internal name in sms_subscriptions
    },
    "Product Updates": {
        "opted_out":      "sms_product_updates_opted_out",
        "opted_out_date": None,
        "subscription_value": "Product Updates",
    },
}


# ---------------------------------------------------------------------------
# Channel → number mapping (built once at module load, not per-invocation)
#
# TWILIO_NUMBER_CHANNELS env var format (set in template.yaml):
#   {"Marketing": ["+12135661157", "+12135613526", "+12135795481"]}
#
# Inverted at runtime to: {"+12135661157": "Marketing", ...}
# ---------------------------------------------------------------------------

def _build_number_channel_map() -> dict:
    raw = os.environ.get("TWILIO_NUMBER_CHANNELS", "{}")
    try:
        channel_to_numbers = json.loads(raw)
    except json.JSONDecodeError:
        print(f"ERROR: TWILIO_NUMBER_CHANNELS is not valid JSON: {raw[:100]}")
        return {}
    return {
        num: channel
        for channel, numbers in channel_to_numbers.items()
        for num in numbers
    }


_NUMBER_CHANNEL_MAP = _build_number_channel_map()


def _channel_for_number(to_number: str) -> str:
    return _NUMBER_CHANNEL_MAP.get(to_number, "Unknown")


# ---------------------------------------------------------------------------
# Per-number Octopods URL map (built once at module load)
#
# OCTOPODS_WEBHOOK_URLS env var format (from Secrets Manager):
#   {"+12135613526": "https://app.octopods.io/...", ...}
#
# Each Twilio number has its own Octopods webhook token — looked up by our
# number on every forward. In Phase 3 (full Octopods replacement) this map
# goes away entirely and all traffic routes through our own outbound Lambda.
#
# NOTE: For inbound messages, our number is the `To` field.
#       For status callbacks, our number is the `From` field (we were the sender).
# ---------------------------------------------------------------------------

def _build_octopods_url_map() -> dict:
    raw = os.environ.get("OCTOPODS_WEBHOOK_URLS", "e30=")  # e30= is base64 for {}
    try:
        decoded = base64.b64decode(raw).decode()
        return json.loads(decoded)
    except Exception as e:
        print(f"ERROR: failed to decode OCTOPODS_WEBHOOK_URLS: {e}")
        return {}


_OCTOPODS_URL_MAP = _build_octopods_url_map()


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def _get_db():
    ctx = ssl.create_default_context()
    return pg8000.connect(
        host=os.environ["DB_HOST"],
        database=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        ssl_context=ctx,
    )


# ---------------------------------------------------------------------------
# Twilio signature validation
# ---------------------------------------------------------------------------

def _compute_twilio_signature(auth_token: str, url: str, params: dict) -> str:
    """Compute the HMAC-SHA1 Twilio signature for the given URL and POST params."""
    s = url + "".join(k + params[k] for k in sorted(params))
    mac = _hmac.new(auth_token.encode(), s.encode(), hashlib.sha1)
    return base64.b64encode(mac.digest()).decode()


def _validate_twilio_signature(auth_token: str, signature: str, url: str, params: dict) -> bool:
    """
    HMAC-SHA1 per Twilio spec:
      1. Start with the full request URL
      2. Append each POST param sorted alphabetically: key + value (no separator)
      3. Sign with HMAC-SHA1 using the auth token, base64-encode
      4. Compare in constant time against the X-Twilio-Signature header
    """
    return _hmac.compare_digest(_compute_twilio_signature(auth_token, url, params), signature)


def _parse_event(event: dict) -> tuple[str, dict, str]:
    """Extract (full_url, params_dict, raw_body) from an API Gateway v2 event."""
    domain = event["requestContext"]["domainName"]
    path   = event["rawPath"]
    url    = f"https://{domain}{path}"

    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode()

    params = dict(urllib.parse.parse_qsl(raw, keep_blank_values=True))
    return url, params, raw


# ---------------------------------------------------------------------------
# HubSpot helpers
# ---------------------------------------------------------------------------

def _hs_headers() -> dict:
    return {
        "Authorization": f"Bearer {os.environ['HUBSPOT_TOKEN']}",
        "Content-Type":  "application/json",
    }


def _phone_candidates(e164: str) -> list[str]:
    """
    Build a list of format variants to try when searching HubSpot.
    Our contact data has mixed formats so we cast a wider net in one API call.
    """
    candidates = [e164]                            # +12135661157
    digits = e164.lstrip("+")
    if digits != e164:
        candidates.append(digits)                  # 12135661157
    if e164.startswith("+1") and len(e164) == 12:
        candidates.append(e164[2:])                # 2135661157 (US 10-digit)
    return candidates


def _find_contact(phone_e164: str) -> str | None:
    """
    Search HubSpot contacts by phone. Tries mobilephone first, then phone as fallback.
    Returns the contact VID (string) or None if not found.
    Each search sends all format variants in one API call via OR'd filterGroups.
    """
    candidates = _phone_candidates(phone_e164)
    for field in ("mobilephone", "phone"):
        payload = {
            "filterGroups": [
                {"filters": [{"propertyName": field, "operator": "EQ", "value": v}]}
                for v in candidates
            ],
            "properties": [],
            "limit": 1,
        }
        try:
            resp = requests.post(
                f"{HUBSPOT_BASE}/crm/v3/objects/contacts/search",
                json=payload,
                headers=_hs_headers(),
                timeout=REQUEST_TIMEOUT,
            )
            if resp.status_code == 200:
                results = resp.json().get("results", [])
                if results:
                    return results[0]["id"]
            else:
                print(f"HubSpot search error ({field}): HTTP {resp.status_code} {resp.text[:200]}")
        except Exception as e:
            print(f"HubSpot search exception ({field}): {e}")
    return None


def _get_contact_subscriptions(contact_id: str) -> list[str]:
    """
    Return current sms_subscriptions values as a list of internal names.
    e.g. ["Marketing", "Product Updates"]
    Returns [] on error (safe default — we won't wipe subscriptions incorrectly).
    """
    try:
        resp = requests.get(
            f"{HUBSPOT_BASE}/crm/v3/objects/contacts/{contact_id}",
            params={"properties": "sms_subscriptions"},
            headers=_hs_headers(),
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code == 200:
            raw = resp.json().get("properties", {}).get("sms_subscriptions") or ""
            return [v.strip() for v in raw.split(";") if v.strip()]
        print(f"Failed to get subscriptions for {contact_id}: HTTP {resp.status_code}")
    except Exception as e:
        print(f"Exception getting subscriptions for {contact_id}: {e}")
    return []


def _update_contact(contact_id: str, props: dict) -> str:
    """PATCH a contact's properties. Returns 'ok' or an error string."""
    try:
        resp = requests.patch(
            f"{HUBSPOT_BASE}/crm/v3/objects/contacts/{contact_id}",
            json={"properties": props},
            headers=_hs_headers(),
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code in (200, 204):
            return "ok"
        return f"HTTP {resp.status_code}: {resp.text[:200]}"
    except Exception as e:
        return str(e)[:200]


def _create_contact_note(contact_id: str, body: str) -> str:
    """
    Create a note on a HubSpot contact timeline. Returns 'ok' or an error string.
    Uses two API calls: POST to create the note, PUT to associate it with the contact.
    """
    now_ms = str(int(time.time() * 1000))
    try:
        resp = requests.post(
            f"{HUBSPOT_BASE}/crm/v3/objects/notes",
            json={"properties": {"hs_note_body": body, "hs_timestamp": now_ms}},
            headers=_hs_headers(),
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code not in (200, 201):
            return f"note_create_failed: HTTP {resp.status_code}"
        note_id = resp.json()["id"]
    except Exception as e:
        return f"note_create_exception: {e}"

    try:
        assoc = requests.put(
            f"{HUBSPOT_BASE}/crm/v3/objects/notes/{note_id}/associations/contacts/{contact_id}/note_to_contact",
            headers=_hs_headers(),
            timeout=REQUEST_TIMEOUT,
        )
        if assoc.status_code not in (200, 201):
            return f"note_assoc_failed: HTTP {assoc.status_code}"
    except Exception as e:
        return f"note_assoc_exception: {e}"

    return "ok"


# ---------------------------------------------------------------------------
# Opt-out / opt-in logic
# ---------------------------------------------------------------------------

def _apply_opt_out(contact_id: str, channel: str, to_number: str) -> str:
    """
    Record a STOP on the given channel:
      - Removes channel from sms_subscriptions
      - Sets sms_*_opted_out = true
      - Sets sms_*_opted_out_date (if configured for this channel)
      - Creates a timeline note
    All property changes are sent in a single PATCH call.
    Returns 'ok' or a combined error string.
    """
    cfg = _CHANNEL_PROPS.get(channel)
    if not cfg:
        return f"no_config_for_channel:{channel}"

    current_subs = _get_contact_subscriptions(contact_id)
    new_subs = [v for v in current_subs if v != cfg["subscription_value"]]

    patch: dict = {
        cfg["opted_out"]:      "true",
        "sms_subscriptions":   ";".join(new_subs),
    }
    if cfg.get("opted_out_date"):
        patch[cfg["opted_out_date"]] = str(int(time.time() * 1000))

    update_status = _update_contact(contact_id, patch)

    note_body = (
        f"Contact replied STOP to {channel} SMS number {to_number}. "
        f"Removed from {channel} SMS subscription."
    )
    note_status = _create_contact_note(contact_id, note_body)

    if update_status == "ok" and note_status == "ok":
        return "ok"
    return f"update={update_status} note={note_status}"


def _apply_opt_in(contact_id: str, channel: str, to_number: str) -> str:
    """
    Record a START on the given channel:
      - Adds channel back to sms_subscriptions (if not already present)
      - Sets sms_*_opted_out = false
      - Creates a timeline note
      - Does NOT clear sms_*_opted_out_date — the history is preserved
    Returns 'ok' or a combined error string.
    """
    cfg = _CHANNEL_PROPS.get(channel)
    if not cfg:
        return f"no_config_for_channel:{channel}"

    current_subs = _get_contact_subscriptions(contact_id)
    if cfg["subscription_value"] not in current_subs:
        current_subs.append(cfg["subscription_value"])

    patch: dict = {
        cfg["opted_out"]:    "false",
        "sms_subscriptions": ";".join(current_subs),
    }

    update_status = _update_contact(contact_id, patch)

    note_body = (
        f"Contact replied START to {channel} SMS number {to_number}. "
        f"Re-enrolled in {channel} SMS subscription."
    )
    note_status = _create_contact_note(contact_id, note_body)

    if update_status == "ok" and note_status == "ok":
        return "ok"
    return f"update={update_status} note={note_status}"


# ---------------------------------------------------------------------------
# Octopods forwarding
# ---------------------------------------------------------------------------

def _forward_to_octopods(raw_body: str, content_type: str, params: dict, our_number: str) -> int | None:
    """
    POST the raw Twilio body to the Octopods URL for the given Twilio number.
    Returns HTTP status code or None if the number isn't mapped or the request fails.

    our_number must be one of our Twilio numbers in E.164 format:
      - For inbound messages: pass the `To` field (our number received the message)
      - For status callbacks: pass the `From` field (our number sent the message)

    The X-Twilio-Signature is re-computed for the Octopods URL. The signature
    Twilio sent was bound to our URL — Octopods validates against their own URL,
    so we must re-sign using the same auth token before forwarding.

    Phase 3 note: once we replace Octopods, this function and _OCTOPODS_URL_MAP
    go away entirely — all traffic routes through our own outbound Lambda instead.
    """
    url = _OCTOPODS_URL_MAP.get(our_number)
    if not url:
        print(f"No Octopods URL configured for {our_number} — skipping forward")
        return None
    try:
        auth_token = os.environ["TWILIO_AUTH_TOKEN"]
        signature  = _compute_twilio_signature(auth_token, url, params)
        resp = requests.post(
            url,
            data=raw_body,
            headers={
                "Content-Type":       content_type,
                "X-Twilio-Signature": signature,
            },
            timeout=10,
        )
        return resp.status_code
    except Exception as e:
        print(f"Octopods forward failed for {our_number}: {e}")
        return None


# ---------------------------------------------------------------------------
# Route: inbound messages
# ---------------------------------------------------------------------------

def _handle_inbound(event: dict, db) -> dict:
    url, params, raw_body = _parse_event(event)
    headers = {k.lower(): v for k, v in event.get("headers", {}).items()}
    sig = headers.get("x-twilio-signature", "")

    if not _validate_twilio_signature(os.environ["TWILIO_AUTH_TOKEN"], sig, url, params):
        print(
            f"Signature validation failed — url={url} "
            f"sig_received={sig[:12]}... "
            f"sig_computed={_compute_twilio_signature(os.environ['TWILIO_AUTH_TOKEN'], url, params)[:12]}... "
            f"param_keys={sorted(params.keys())} "
            f"body_raw={params.get('Body','')!r}"
        )
        return {"statusCode": 403, "body": "Forbidden"}

    message_sid   = params.get("MessageSid", "")
    from_number   = params.get("From", "")
    to_number     = params.get("To", "")
    body_text     = (params.get("Body") or "").strip()
    keyword_upper = body_text.upper()
    channel       = _channel_for_number(to_number)

    is_opt_out      = keyword_upper in OPT_OUT_KEYWORDS
    is_opt_in       = keyword_upper in OPT_IN_KEYWORDS
    opt_out_keyword = keyword_upper if is_opt_out else None
    opt_in_keyword  = keyword_upper if is_opt_in  else None

    contact_id       = None
    hs_update_status = None

    if is_opt_out or is_opt_in:
        contact_id = _find_contact(from_number)
        if contact_id:
            if is_opt_out:
                hs_update_status = _apply_opt_out(contact_id, channel, to_number)
                print(f"Opt-out '{keyword_upper}' from {from_number} channel={channel} to={to_number}: {hs_update_status}")
            else:
                hs_update_status = _apply_opt_in(contact_id, channel, to_number)
                print(f"Opt-in '{keyword_upper}' from {from_number} channel={channel} to={to_number}: {hs_update_status}")
        else:
            hs_update_status = "contact_not_found"
            print(f"No HubSpot contact found for {from_number}")

    # to_number is our Twilio number for inbound messages
    octopods_code = _forward_to_octopods(
        raw_body,
        headers.get("content-type", "application/x-www-form-urlencoded"),
        params=params,
        our_number=to_number,
    )

    cur = db.cursor()
    cur.execute(
        """
        INSERT INTO sms_inbound_message
            (message_sid, from_number, to_number, body, channel,
             opt_out_keyword, opt_in_keyword,
             hubspot_contact_id, hubspot_update_status,
             forwarded_to_octopods, octopods_status_code, raw_payload)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (message_sid) DO NOTHING
        """,
        (
            message_sid, from_number, to_number, body_text, channel,
            opt_out_keyword, opt_in_keyword,
            contact_id, hs_update_status,
            octopods_code is not None, octopods_code,
            json.dumps(params),
        ),
    )
    db.commit()

    # Empty TwiML — Twilio handles STOP auto-replies itself at the carrier level.
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "text/xml"},
        "body": "<?xml version='1.0' encoding='UTF-8'?><Response/>",
    }


# ---------------------------------------------------------------------------
# Route: delivery status callbacks
# ---------------------------------------------------------------------------

def _handle_status(event: dict, db) -> dict:
    """
    Process Twilio delivery status callbacks.

    Phase 1 scope: validate the request, update HubSpot on hard failures, return 200.
    No DB writes — we don't control outbound sends yet so there's no message row to
    update and no useful body/sent_at to store. Full outbound message tracking comes
    in Phase 3 when we own the send path.
    """
    url, params, _ = _parse_event(event)
    headers  = {k.lower(): v for k, v in event.get("headers", {}).items()}
    sig      = headers.get("x-twilio-signature", "")

    if not _validate_twilio_signature(os.environ["TWILIO_AUTH_TOKEN"], sig, url, params):
        print(f"Signature validation failed — possible spoofed request to {url}")
        return {"statusCode": 403, "body": "Forbidden"}

    error_code = params.get("ErrorCode") or None
    to_number  = params.get("To", "")

    if error_code in HARD_FAILURE_CODES:
        reason = HARD_FAILURE_CODES[error_code]
        print(f"Hard delivery failure: error_code={error_code} ({reason}) to={to_number}")
        contact_id = _find_contact(to_number)
        if contact_id:
            status = _update_contact(contact_id, {
                "sms_deliverable":       "false",
                "sms_ineligible_reason": reason,
            })
            print(f"Contact {contact_id}: sms_deliverable=false reason={reason} — {status}")
        else:
            print(f"No HubSpot contact found for {to_number}")

    return {"statusCode": 200, "body": ""}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def lambda_handler(event, context):
    path = event.get("rawPath", "")

    # Status callbacks don't touch the DB in Phase 1 — no connection needed.
    if path.endswith("/status"):
        try:
            return _handle_status(event, None)
        except Exception as e:
            print(f"Unhandled exception on {path}: {e}")
            return {"statusCode": 200, "body": ""}

    # Inbound messages need the DB.
    db = _get_db()
    try:
        if path.endswith("/inbound"):
            return _handle_inbound(event, db)
        return {"statusCode": 404, "body": "Not found"}
    except Exception as e:
        # Return 200 so Twilio does not retry — retries cause duplicate processing.
        print(f"Unhandled exception on {path}: {e}")
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "text/xml"},
            "body": "<?xml version='1.0' encoding='UTF-8'?><Response/>",
        }
    finally:
        db.close()
