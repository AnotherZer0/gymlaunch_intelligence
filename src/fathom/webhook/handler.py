"""
Fathom webhook Lambda
Receives call transcript payloads fired by Zapier and writes them to RDS.
"""

import base64
import hmac
import json
import os
import ssl

import pg8000


def parse_bool(val):
    """
    Coerce a value to bool or None, handling Zapier's quirks.
    Zapier sometimes serializes boolean fields as "True", "False", or comma-joined
    strings like "True,True" when a field has multiple values. We take the first
    token and normalize it.
    """
    if val is None:
        return None
    if isinstance(val, bool):
        return val
    first = str(val).split(",")[0].strip().lower()
    if first in ("true", "1", "yes"):
        return True
    if first in ("false", "0", "no"):
        return False
    return None


def get_db_connection():
    ctx = ssl.create_default_context()
    return pg8000.connect(
        host=os.environ["DB_HOST"],
        port=5432,
        database=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        ssl_context=ctx,
        timeout=10,
    )


def lambda_handler(event, context):
    print("Fathom webhook received")

    received = (event.get("headers") or {}).get("x-webhook-secret", "")
    expected = os.environ.get("WEBHOOK_SECRET", "")
    if not hmac.compare_digest(received, expected):
        print("Invalid webhook secret")
        return {"statusCode": 403, "body": "forbidden"}

    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")

    try:
        payload = json.loads(body)
    except Exception as e:
        print(f"Failed to parse body: {e}")
        return {"statusCode": 400, "body": "invalid json"}

    fathom_id = payload.get("fathom_id")
    if not fathom_id:
        print("Missing fathom_id")
        return {"statusCode": 400, "body": "missing fathom_id"}

    conn = get_db_connection()
    try:
        cur = conn.cursor()

        duration = payload.get("recording_duration_in_minutes")
        scheduled_duration = payload.get("meeting_scheduled_duration_in_minutes")

        cur.execute(
            """
            INSERT INTO fathom_call (
                fathom_id,
                meeting_title,
                fathom_user_name,
                fathom_user_email,
                fathom_user_team,
                meeting_scheduled_start_time,
                meeting_scheduled_end_time,
                meeting_scheduled_duration_in_minutes,
                recording_duration_in_minutes,
                recording_url,
                recording_share_url,
                meeting_join_url,
                transcript_plaintext,
                raw_payload
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (fathom_id) DO UPDATE SET
                meeting_title                          = EXCLUDED.meeting_title,
                fathom_user_name                       = EXCLUDED.fathom_user_name,
                fathom_user_email                      = EXCLUDED.fathom_user_email,
                fathom_user_team                       = EXCLUDED.fathom_user_team,
                meeting_scheduled_start_time           = EXCLUDED.meeting_scheduled_start_time,
                meeting_scheduled_end_time             = EXCLUDED.meeting_scheduled_end_time,
                meeting_scheduled_duration_in_minutes  = EXCLUDED.meeting_scheduled_duration_in_minutes,
                recording_duration_in_minutes          = EXCLUDED.recording_duration_in_minutes,
                recording_url                          = EXCLUDED.recording_url,
                recording_share_url                    = EXCLUDED.recording_share_url,
                meeting_join_url                       = EXCLUDED.meeting_join_url,
                transcript_plaintext                   = EXCLUDED.transcript_plaintext,
                raw_payload                            = EXCLUDED.raw_payload
            RETURNING id
            """,
            (
                fathom_id,
                payload.get("meeting_title"),
                payload.get("fathom_user_name"),
                payload.get("fathom_user_email"),
                payload.get("fathom_user_team"),
                payload.get("meeting_scheduled_start_time") or None,
                payload.get("meeting_scheduled_end_time") or None,
                str(scheduled_duration) if scheduled_duration is not None else None,
                float(duration) if duration is not None else None,
                payload.get("recording_url"),
                payload.get("recording_share_url"),
                payload.get("meeting_join_url"),
                payload.get("transcript_plaintext"),
                json.dumps(payload),
            ),
        )
        call_id = cur.fetchone()[0]

        # Build invitee list. The webhook flattens the primary invitee into singular fields;
        # meeting_invitees is almost always empty but we handle it if present.
        invitees = []
        primary_email = payload.get("meeting_invitees_email")
        if primary_email:
            invitees.append({
                "name": payload.get("meeting_invitees_name"),
                "email": primary_email,
                "is_external": parse_bool(payload.get("meeting_invitees_is_external")),
                "domain_name": payload.get("meeting_external_domains_domain_name"),
            })

        for entry in (payload.get("meeting_invitees") or []):
            email = entry.get("email") if isinstance(entry, dict) else str(entry)
            if email and email != primary_email:
                invitees.append({
                    "name": entry.get("name") if isinstance(entry, dict) else None,
                    "email": email,
                    "is_external": None,
                    "domain_name": None,
                })

        for inv in invitees:
            cur.execute(
                """
                INSERT INTO fathom_call_invitee (call_id, name, email, is_external, domain_name)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (call_id, email) DO UPDATE SET
                    name        = EXCLUDED.name,
                    is_external = EXCLUDED.is_external,
                    domain_name = EXCLUDED.domain_name
                """,
                (call_id, inv["name"], inv["email"], inv["is_external"], inv["domain_name"]),
            )

        conn.commit()
        cur.close()
        print(f"Stored fathom_id={fathom_id} call_id={call_id} invitees={len(invitees)}")
        return {"statusCode": 200, "body": "ok"}

    except Exception as e:
        print(f"Error storing fathom_id={fathom_id}: {e}")
        conn.rollback()
        return {"statusCode": 500, "body": "internal error"}

    finally:
        conn.close()
