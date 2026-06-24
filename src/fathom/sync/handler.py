"""
Fathom daily sync Lambda — gymlaunch-fathom-daily-sync

Pulls meetings from the Fathom API and upserts them into fathom_call + fathom_call_invitee.

Normal run (scheduled, FULL_SYNC=false):
  Fetches meetings created in the last 48 hours. Both the webhook and this sync may
  write the same call; the upsert on fathom_id makes both paths safe to run concurrently.

Full sync (FULL_SYNC=true env var):
  Fetches ALL meetings with no date filter. Skips any meeting already in the DB with a
  transcript so we don't re-download data we already have. Set this for the first run to
  backfill historical calls, then set it back to false.
"""

import json
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import pg8000


FATHOM_API_BASE = "https://api.fathom.ai/external/v1"

# Fathom allows 60 requests/minute. Sleep this long between page fetches to stay
# comfortably under the limit (1 req/sec = 60/min). Cheap insurance against 429s.
THROTTLE_SECONDS = 1.1
# How many times to retry a single request that hits 429 / transient 5xx.
MAX_RETRIES = 5


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


def fathom_get(path, params=None):
    url = f"{FATHOM_API_BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {
        "X-Api-Key": os.environ["FATHOM_API_KEY"],
        "Accept": "application/json",
    }

    for attempt in range(MAX_RETRIES + 1):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            # 429 = rate limit; 5xx = transient server error. Both are worth retrying.
            if e.code in (429, 500, 502, 503, 504) and attempt < MAX_RETRIES:
                retry_after = e.headers.get("Retry-After")
                if retry_after and str(retry_after).isdigit():
                    wait = int(retry_after)
                else:
                    wait = min(2 ** attempt, 30)  # exponential backoff, capped at 30s
                print(f"  Fathom {e.code}; retry {attempt + 1}/{MAX_RETRIES} after {wait}s")
                time.sleep(wait)
                continue
            raise


def build_transcript_text(transcript_items):
    if not transcript_items:
        return None
    lines = []
    for item in transcript_items:
        speaker = (item.get("speaker") or {}).get("display_name") or "Unknown"
        text = (item.get("text") or "").strip()
        if text:
            lines.append(f"{speaker}: {text}")
    return "\n".join(lines) or None


def get_ids_with_transcript(conn, fathom_ids):
    """Return set of fathom_ids already in DB that have a non-null transcript."""
    if not fathom_ids:
        return set()
    cur = conn.cursor()
    cur.execute(
        "SELECT fathom_id FROM fathom_call "
        "WHERE fathom_id = ANY(%s) AND transcript_plaintext IS NOT NULL",
        (list(fathom_ids),),
    )
    rows = cur.fetchall()
    cur.close()
    return {row[0] for row in rows}


def calc_duration_minutes(start_str, end_str):
    if not start_str or not end_str:
        return None
    try:
        # Fathom returns ISO 8601 — strip trailing Z for fromisoformat compat
        start = datetime.fromisoformat(start_str.rstrip("Z"))
        end = datetime.fromisoformat(end_str.rstrip("Z"))
        return round((end - start).total_seconds() / 60, 2)
    except Exception:
        return None


def upsert_meeting(conn, meeting, transcript_text):
    fathom_id = meeting["recording_id"]
    recorded_by = meeting.get("recorded_by") or {}
    team = recorded_by.get("team")
    team_name = team.get("name") if isinstance(team, dict) else (team if isinstance(team, str) else None)

    sched_duration = calc_duration_minutes(
        meeting.get("scheduled_start_time"), meeting.get("scheduled_end_time")
    )
    rec_duration = calc_duration_minutes(
        meeting.get("recording_start_time"), meeting.get("recording_end_time")
    )

    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO fathom_call (
            fathom_id, meeting_title, fathom_user_name, fathom_user_email,
            fathom_user_team, meeting_scheduled_start_time, meeting_scheduled_end_time,
            meeting_scheduled_duration_in_minutes, recording_duration_in_minutes,
            recording_url, recording_share_url, meeting_join_url,
            transcript_plaintext, raw_payload
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (fathom_id) DO UPDATE SET
            meeting_title                         = EXCLUDED.meeting_title,
            fathom_user_name                      = EXCLUDED.fathom_user_name,
            fathom_user_email                     = EXCLUDED.fathom_user_email,
            fathom_user_team                      = EXCLUDED.fathom_user_team,
            meeting_scheduled_start_time          = EXCLUDED.meeting_scheduled_start_time,
            meeting_scheduled_end_time            = EXCLUDED.meeting_scheduled_end_time,
            meeting_scheduled_duration_in_minutes = EXCLUDED.meeting_scheduled_duration_in_minutes,
            recording_duration_in_minutes         = EXCLUDED.recording_duration_in_minutes,
            recording_url                         = EXCLUDED.recording_url,
            recording_share_url                   = EXCLUDED.recording_share_url,
            meeting_join_url                      = EXCLUDED.meeting_join_url,
            transcript_plaintext                  = COALESCE(EXCLUDED.transcript_plaintext, fathom_call.transcript_plaintext),
            raw_payload                           = EXCLUDED.raw_payload
        RETURNING id
        """,
        (
            fathom_id,
            meeting.get("title") or meeting.get("meeting_title"),
            recorded_by.get("name"),
            recorded_by.get("email"),
            team_name,
            meeting.get("scheduled_start_time") or None,
            meeting.get("scheduled_end_time") or None,
            str(sched_duration) if sched_duration is not None else None,
            rec_duration,
            meeting.get("url"),
            meeting.get("share_url"),
            meeting.get("meeting_url"),
            transcript_text,
            json.dumps(meeting),
        ),
    )
    call_id = cur.fetchone()[0]

    invitees = meeting.get("calendar_invitees") or []
    for inv in invitees:
        email = inv.get("email")
        if not email:
            continue
        cur.execute(
            """
            INSERT INTO fathom_call_invitee (call_id, name, email, is_external, domain_name)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (call_id, email) DO UPDATE SET
                name        = EXCLUDED.name,
                is_external = EXCLUDED.is_external,
                domain_name = EXCLUDED.domain_name
            """,
            (call_id, inv.get("name"), email, inv.get("is_external"), inv.get("email_domain")),
        )

    cur.close()
    return call_id


def lambda_handler(event, context):
    full_sync = os.environ.get("FULL_SYNC", "false").lower() == "true"
    print(f"Fathom daily sync starting — full_sync={full_sync}")

    conn = get_db_connection()
    stored = skipped = errors = 0

    try:
        params = {"include_transcript": "true"}
        if not full_sync:
            after = (datetime.now(timezone.utc) - timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%SZ")
            params["created_after"] = after
            print(f"Window: meetings created after {after}")
        else:
            print("Full sync: fetching all meetings, will skip those already stored with transcript")

        cursor = None

        while True:
            page_params = dict(params)
            if cursor:
                page_params["cursor"] = cursor

            page = fathom_get("/meetings", page_params)
            meetings = page.get("items") or []

            if not meetings:
                break

            # Batch DB check: skip meetings we already have a transcript for
            ids = [m.get("recording_id") for m in meetings if m.get("recording_id")]
            already_have = get_ids_with_transcript(conn, ids)

            for meeting in meetings:
                fathom_id = meeting.get("recording_id")
                if not fathom_id:
                    print(f"  WARNING: meeting missing recording_id ({meeting.get('title')!r}), skipping")
                    continue

                if fathom_id in already_have:
                    skipped += 1
                    continue

                transcript_items = meeting.get("transcript") or []
                transcript_text = build_transcript_text(transcript_items)

                try:
                    call_id = upsert_meeting(conn, meeting, transcript_text)
                    conn.commit()
                    stored += 1
                    invitee_count = len(meeting.get("calendar_invitees") or [])
                    has_transcript = transcript_text is not None
                    print(f"  stored fathom_id={fathom_id} call_id={call_id} invitees={invitee_count} transcript={has_transcript}")
                except Exception as e:
                    conn.rollback()
                    errors += 1
                    print(f"  ERROR fathom_id={fathom_id}: {e}")

            # Cursor-based pagination
            cursor = page.get("next_cursor")
            if not cursor:
                break

            # Stay under Fathom's 60 req/min limit before fetching the next page.
            time.sleep(THROTTLE_SECONDS)

    finally:
        conn.close()

    summary = f"stored={stored} skipped={skipped} errors={errors}"
    print(f"Fathom sync complete — {summary}")
    return {"statusCode": 200, "stored": stored, "skipped": skipped, "errors": errors}
