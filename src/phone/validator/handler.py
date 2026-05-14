"""
Phone Validator Lambda
POST /phone/validate

Accepts raw phone and mobilephone values, normalizes them using Google's
libphonenumber (phonenumbers library), applies a promotion/normalization
decision, then runs a single Twilio Lookup v2 on the winning number.

Returns structured JSON the HubSpot workflow uses to:
  - Update phone/mobilephone fields (should_update_* + new_*_value)
  - Branch on decision_reason, singular_route, path_*_update
  - Build a timeline note from the returned values
  - Determine SMS eligibility from valid + line_type

No HubSpot API calls are made here — the Lambda is stateless.
All CRM updates and note creation are handled in the calling workflow.

--- Input (JSON body) ---
  phone        raw value of the HubSpot `phone` field
  mobilephone  raw value of the HubSpot `mobilephone` field

--- Output (JSON) ---
  See OUTPUT_FIELD_NAMES below for the full list.
  Register these exact keys in the HubSpot code action's "Returns" section.

--- Secrets used ---
  TWILIO_ACCOUNT_SID   env var (from gymlaunch/twilio/account_sid)
  TWILIO_AUTH_TOKEN    env var (from gymlaunch/twilio/auth_token)
"""

import json
import os

import phonenumbers
import requests


TWILIO_LOOKUP_BASE = "https://lookups.twilio.com/v2/PhoneNumbers"
REQUEST_TIMEOUT = 8  # seconds


# ---------------------------------------------------------------------------
# Decision constants
# Register these exact strings in HubSpot workflow IF branches.
# ---------------------------------------------------------------------------

DECISION_NOOP                                   = "noop"
DECISION_NOOP_NO_USABLE                         = "noop_no_usable"
DECISION_NORMALIZE_MOBILE_ONLY                  = "normalize_mobile_only"
DECISION_NORMALIZE_PHONE_ONLY                   = "normalize_phone_only"
DECISION_NORMALIZE_BOTH                         = "normalize_both"
DECISION_PROMOTE_MOBILE_TO_PHONE                = "promote_mobile_to_phone"
DECISION_PROMOTE_MOBILE_TO_PHONE_NORMALIZE_MOBILE = "promote_mobile_to_phone_normalize_mobile"

# singular_route tokens — one branch covers all four update combinations.
SINGULAR_MOBILE_NO_PHONE_NO   = "singular_mobile_no_phone_no"
SINGULAR_MOBILE_NO_PHONE_YES  = "singular_mobile_no_phone_yes"
SINGULAR_MOBILE_YES_PHONE_NO  = "singular_mobile_yes_phone_no"
SINGULAR_MOBILE_YES_PHONE_YES = "singular_mobile_yes_phone_yes"

# HubSpot-safe booleans (strings, not Python bools)
HUB_TRUE  = "true"
HUB_FALSE = "false"
HUB_YES   = "yes"
HUB_NO    = "no"

# All keys returned in outputFields — register these in the HubSpot GUI.
OUTPUT_FIELD_NAMES = (
    "phone_status",
    "phone_reason",
    "phone_normalized",
    "mobile_status",
    "mobile_reason",
    "mobile_normalized",
    "should_update_phone",
    "new_phone_value",
    "should_update_mobile",
    "new_mobile_value",
    "path_mobile_update",
    "path_phone_update",
    "singular_route",
    "decision_reason",
    "winner_e164",
    "valid",
    "line_type",
    "lookup_country",
    "lookup_error",
)


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def classify(raw: str) -> dict:
    """
    Parse and validate a phone number using Google's libphonenumber.

    Default region "US" handles the most common dirty inputs:
      - 10-digit bare numbers (2135661157  → +12135661157)
      - 11-digit with leading 1 (12135661157 → +12135661157)
      - International with + already present (+442071234567 → unchanged)
      - International missing + (442071234567 → +442071234567)

    Returns:
      valid    bool
      e164     E.164 string, or "" if unparseable / invalid
      country  ISO 3166-1 alpha-2 (from libphonenumber), or ""
      reason   short string for logging / HubSpot branching
    """
    if not raw or not raw.strip():
        return {"valid": False, "e164": "", "country": "", "reason": "blank"}
    try:
        num = phonenumbers.parse(raw.strip(), "US")
        if phonenumbers.is_valid_number(num):
            e164    = phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.E164)
            country = phonenumbers.region_code_for_number(num) or ""
            return {"valid": True, "e164": e164, "country": country, "reason": "ok"}
        return {"valid": False, "e164": "", "country": "", "reason": "invalid_number"}
    except phonenumbers.NumberParseException as exc:
        # exc.error_type is an int enum; map to a readable string
        reason_map = {
            0: "invalid_country_code",
            1: "not_a_number",
            2: "too_short_after_idd",
            3: "too_short_nsn",
            4: "too_long",
        }
        reason = reason_map.get(exc.error_type, "parse_error")
        return {"valid": False, "e164": "", "country": "", "reason": reason}


def needs_update(raw: str, classified: dict) -> bool:
    """True if the number parsed successfully but isn't already in E.164 form."""
    if not classified["valid"]:
        return False
    return classified["e164"] != (raw or "").strip()


# ---------------------------------------------------------------------------
# Decision logic
# ---------------------------------------------------------------------------

def decide(phone_raw: str, mobile_raw: str) -> dict:
    """
    Classify both numbers and decide what (if anything) needs updating.

    Priority:
      - If phone is unusable, promote mobile → phone (and normalize mobile if needed)
      - If phone is usable, normalize whichever fields need it
      - winner_e164 is the number we run Twilio Lookup on
    """
    p = classify(phone_raw)
    m = classify(mobile_raw)

    p_update = needs_update(phone_raw, p)
    m_update = needs_update(mobile_raw, m)

    result = {
        "phone_status":     "valid" if p["valid"] else "invalid",
        "phone_reason":     p["reason"],
        "phone_normalized": p["e164"],
        "mobile_status":    "valid" if m["valid"] else "invalid",
        "mobile_reason":    m["reason"],
        "mobile_normalized": m["e164"],
        "should_update_phone":  HUB_FALSE,
        "new_phone_value":      "",
        "should_update_mobile": HUB_FALSE,
        "new_mobile_value":     "",
        "decision_reason":  "",
        "winner_e164":      "",
    }

    if not p["valid"] and not m["valid"]:
        result["decision_reason"] = DECISION_NOOP_NO_USABLE

    elif not p["valid"] and m["valid"]:
        result["should_update_phone"] = HUB_TRUE
        result["new_phone_value"]     = m["e164"]
        result["winner_e164"]         = m["e164"]
        if m_update:
            result["should_update_mobile"] = HUB_TRUE
            result["new_mobile_value"]     = m["e164"]
            result["decision_reason"]      = DECISION_PROMOTE_MOBILE_TO_PHONE_NORMALIZE_MOBILE
        else:
            result["decision_reason"] = DECISION_PROMOTE_MOBILE_TO_PHONE

    else:
        # Phone is valid — it anchors the lookup
        result["winner_e164"] = p["e164"]
        if p_update and m_update:
            result["should_update_phone"]  = HUB_TRUE
            result["new_phone_value"]      = p["e164"]
            result["should_update_mobile"] = HUB_TRUE
            result["new_mobile_value"]     = m["e164"]
            result["decision_reason"]      = DECISION_NORMALIZE_BOTH
        elif p_update:
            result["should_update_phone"] = HUB_TRUE
            result["new_phone_value"]     = p["e164"]
            result["decision_reason"]     = DECISION_NORMALIZE_PHONE_ONLY
        elif m_update:
            result["should_update_mobile"] = HUB_TRUE
            result["new_mobile_value"]     = m["e164"]
            result["decision_reason"]      = DECISION_NORMALIZE_MOBILE_ONLY
        else:
            result["decision_reason"] = DECISION_NOOP

    _apply_path_fields(result)
    return result


def _apply_path_fields(result: dict) -> None:
    """
    Populate path_mobile_update, path_phone_update, singular_route.
    These drive the HubSpot workflow branching without needing multiple IF steps.
    """
    m = result["should_update_mobile"] == HUB_TRUE
    p = result["should_update_phone"]  == HUB_TRUE
    result["path_mobile_update"] = HUB_YES if m else HUB_NO
    result["path_phone_update"]  = HUB_YES if p else HUB_NO
    if   not m and not p: result["singular_route"] = SINGULAR_MOBILE_NO_PHONE_NO
    elif not m and p:     result["singular_route"] = SINGULAR_MOBILE_NO_PHONE_YES
    elif m and not p:     result["singular_route"] = SINGULAR_MOBILE_YES_PHONE_NO
    else:                 result["singular_route"] = SINGULAR_MOBILE_YES_PHONE_YES


# ---------------------------------------------------------------------------
# Twilio Lookup v2
# ---------------------------------------------------------------------------

def twilio_lookup(e164: str) -> dict:
    """
    Run Twilio Lookup v2 on a single E.164 number.
    Returns valid, line_type, lookup_country, lookup_error.
    """
    if not e164:
        return {"valid": "", "line_type": "", "lookup_country": "", "lookup_error": "no_winner"}

    try:
        resp = requests.get(
            f"{TWILIO_LOOKUP_BASE}/{e164}",
            params={"Fields": "line_type_intelligence"},
            auth=(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"]),
            timeout=REQUEST_TIMEOUT,
        )
        data = resp.json()
        if resp.status_code < 300:
            lti = data.get("line_type_intelligence") or {}
            return {
                "valid":          str(data.get("valid", "")).lower(),
                "line_type":      lti.get("type", ""),
                "lookup_country": data.get("country_code", ""),
                "lookup_error":   "",
            }
        err = data.get("message") or data.get("code") or ""
        print(f"Twilio Lookup error for {e164}: HTTP {resp.status_code} {err}")
        return {"valid": "false", "line_type": "", "lookup_country": "", "lookup_error": f"HTTP {resp.status_code}: {err}"}
    except Exception as exc:
        print(f"Twilio Lookup exception for {e164}: {exc}")
        return {"valid": "false", "line_type": "", "lookup_country": "", "lookup_error": str(exc)[:200]}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def lambda_handler(event, context):
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return {"statusCode": 400, "body": json.dumps({"error": "invalid JSON"})}

    phone_raw  = (body.get("phone")       or "").strip()
    mobile_raw = (body.get("mobilephone") or "").strip()

    decision = decide(phone_raw, mobile_raw)
    lookup   = twilio_lookup(decision["winner_e164"])

    output = {
        # Normalization
        "phone_status":     decision["phone_status"],
        "phone_reason":     decision["phone_reason"],
        "phone_normalized": decision["phone_normalized"],
        "mobile_status":    decision["mobile_status"],
        "mobile_reason":    decision["mobile_reason"],
        "mobile_normalized": decision["mobile_normalized"],
        # What to write back
        "should_update_phone":  decision["should_update_phone"],
        "new_phone_value":      decision["new_phone_value"],
        "should_update_mobile": decision["should_update_mobile"],
        "new_mobile_value":     decision["new_mobile_value"],
        # Workflow routing
        "path_mobile_update": decision["path_mobile_update"],
        "path_phone_update":  decision["path_phone_update"],
        "singular_route":     decision["singular_route"],
        "decision_reason":    decision["decision_reason"],
        "winner_e164":        decision["winner_e164"],
        # Twilio Lookup
        "valid":          lookup["valid"],
        "line_type":      lookup["line_type"],
        "lookup_country": lookup["lookup_country"],
        "lookup_error":   lookup["lookup_error"],
    }

    print(
        f"phone='{phone_raw}' mobile='{mobile_raw}' "
        f"decision={decision['decision_reason']} winner={decision['winner_e164']} "
        f"valid={lookup['valid']} line_type={lookup['line_type']}"
    )

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(output),
    }
