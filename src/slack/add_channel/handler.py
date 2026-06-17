"""
gymlaunch-add-slack-channel — Function URL endpoint (add-only)

Registers a Slack channel into the sync database (slack_channel, active=true) so
the hourly Slack sync (gymlaunch-slack-sync) starts pulling its history.

Intended trigger: a HubSpot workflow / custom-code action fires a Slack channel
id at this function's URL when a property is updated. The response is a short,
single-line text (<= 256 chars) meant to be mapped back into a HubSpot
single-line-text property.

AUTH
  The request must present the shared secret, matching env CHANNEL_ADD_API_KEY,
  as EITHER the `x-api-key` header OR a `key` query-string parameter.

INPUT (any one of)
  - query string:  ?channel=C0123456789   (or ?channel_id=)
  - JSON body:     {"channel": "C0123456789"}   (or channel_id)
  - raw body:      C0123456789
  The bot must already be a member of the channel — this endpoint does NOT
  auto-join. If it isn't, the response says so and nothing is registered.

RESPONSE
  Always HTTP 200 with a one-line text body so the caller can map it to a
  property regardless of status. Outcome is encoded in the text ("OK:" / "ERROR:").
  (If you'd rather fail loudly, change the status codes in reply() below.)
"""

import base64
import json
import os
import ssl

import pg8000
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

MAX_RESPONSE_CHARS = 256


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


def get_slack_client() -> WebClient:
    return WebClient(token=os.environ["SLACK_BOT_TOKEN"], timeout=30)


# --- Request / response helpers ---

def reply(message: str, status: int = 200) -> dict:
    """One-line, <=256 char plaintext response (HubSpot maps it to a property)."""
    one_line = " ".join(str(message).split())  # collapse newlines/extra spaces
    return {
        "statusCode": status,
        "headers": {"Content-Type": "text/plain; charset=utf-8"},
        "body": one_line[:MAX_RESPONSE_CHARS],
    }


def extract_inputs(event: dict):
    """Return (api_key, channel_id) from a Lambda Function URL event."""
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    qs = event.get("queryStringParameters") or {}

    api_key = headers.get("x-api-key") or qs.get("key")

    channel = qs.get("channel") or qs.get("channel_id")
    if not channel:
        raw = event.get("body") or ""
        if event.get("isBase64Encoded"):
            raw = base64.b64decode(raw).decode("utf-8", "replace")
        raw = raw.strip()
        if raw:
            try:
                data = json.loads(raw)
                channel = data.get("channel") or data.get("channel_id") if isinstance(data, dict) else None
            except (ValueError, TypeError):
                channel = raw  # not JSON — treat the whole body as the channel id

    return api_key, (channel or "").strip()


# --- Entry point ---

def lambda_handler(event, context):
    expected = os.environ.get("CHANNEL_ADD_API_KEY", "")
    api_key, channel_id = extract_inputs(event)

    if not expected or api_key != expected:
        return reply("ERROR: unauthorized (bad or missing key)")

    if not channel_id:
        return reply("ERROR: no channel id provided")

    slack = get_slack_client()

    try:
        channel = slack.conversations_info(channel=channel_id)["channel"]
    except SlackApiError as e:
        return reply(f"ERROR: cannot read {channel_id}: {e.response.get('error', 'slack_error')}")
    except Exception as e:  # network / unexpected
        return reply(f"ERROR: slack lookup failed for {channel_id}: {e}")

    ch_id = channel["id"]
    ch_name = channel.get("name", ch_id)

    if not channel.get("is_member", False):
        return reply(f"ERROR: bot not in #{ch_name} ({ch_id}) — invite the bot, then retry")

    try:
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO slack_channel (channel_id, name, active)
                VALUES (%s, %s, true)
                ON CONFLICT (channel_id) DO UPDATE SET
                    name   = EXCLUDED.name,
                    active = true
                """,
                (ch_id, ch_name),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        return reply(f"ERROR: db write failed for #{ch_name} ({ch_id}): {e}")

    return reply(f"OK: #{ch_name} ({ch_id}) registered for syncing")
