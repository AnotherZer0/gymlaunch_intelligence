"""
Slack sync Lambda
Pulls messages from all active Slack channels into RDS.
Triggered by EventBridge on a schedule.
"""

import json
import os
import ssl
import pg8000
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from datetime import datetime, timezone


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
        timeout=10,
    )


def get_slack_client() -> WebClient:
    return WebClient(token=os.environ["SLACK_BOT_TOKEN"], timeout=30)


# --- Internal domain classification ---

INTERNAL_DOMAINS = [
    "gymlaunch.com",
    "gymlaunchsecrets.com",
    "gymowners.com",
]


def is_internal_email(email: str) -> bool:
    if not email:
        return False
    return any(email.lower().endswith(f"@{domain}") for domain in INTERNAL_DOMAINS)


# --- Upsert helpers ---

def upsert_user(cur, user: dict) -> None:
    """
    Insert or update a Slack user.
    Respects classification_locked — never overwrites is_internal if locked.
    """
    profile = user.get("profile", {})
    email = profile.get("email", "")

    cur.execute(
        """
        INSERT INTO slack_user (
            user_id, name, display_name, email,
            is_internal, is_bot, updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, now())
        ON CONFLICT (user_id) DO UPDATE SET
            name         = EXCLUDED.name,
            display_name = EXCLUDED.display_name,
            email        = EXCLUDED.email,
            is_internal  = CASE
                WHEN slack_user.classification_locked THEN slack_user.is_internal
                ELSE EXCLUDED.is_internal
            END,
            is_bot       = EXCLUDED.is_bot,
            updated_at   = now()
        """,
        (
            user["id"],
            user.get("real_name") or user.get("name", ""),
            profile.get("display_name", ""),
            email,
            is_internal_email(email),
            user.get("is_bot", False),
        ),
    )


def upsert_message(cur, msg: dict, channel_id: str) -> None:
    """
    Insert or update a Slack message.
    On conflict (same channel + ts) updates text and raw_payload only —
    preserves original ingestion timestamp.
    """
    ts = msg["ts"]
    thread_ts = msg.get("thread_ts")
    is_reply = bool(thread_ts and thread_ts != ts)
    posted_at = datetime.fromtimestamp(float(ts), tz=timezone.utc)

    cur.execute(
        """
        INSERT INTO slack_message (
            channel_id, user_id, ts, thread_ts,
            is_thread_reply, text, posted_at, raw_payload
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (channel_id, ts) DO UPDATE SET
            text        = EXCLUDED.text,
            raw_payload = EXCLUDED.raw_payload
        """,
        (
            channel_id,
            msg.get("user"),
            ts,
            thread_ts,
            is_reply,
            msg.get("text", ""),
            posted_at,
            json.dumps(msg),
        ),
    )


def update_sync_state(
    cur, channel_id: str, last_ts: str | None,
    status: str = "ok", error_message: str | None = None
) -> None:
    cur.execute(
        """
        INSERT INTO slack_sync_state (
            channel_id, last_ts, last_synced_at, status, error_message, updated_at
        )
        VALUES (%s, %s, now(), %s, %s, now())
        ON CONFLICT (channel_id) DO UPDATE SET
            last_ts       = EXCLUDED.last_ts,
            last_synced_at = now(),
            status        = EXCLUDED.status,
            error_message = EXCLUDED.error_message,
            updated_at    = now()
        """,
        (channel_id, last_ts, status, error_message),
    )


# --- Channel sync ---

def sync_channel(cur, slack: WebClient, channel_id: str, channel_name: str, known_users: set) -> None:
    """Sync all messages for one channel since the last run."""

    # Find where we left off
    cur.execute(
        "SELECT last_ts FROM slack_sync_state WHERE channel_id = %s",
        (channel_id,)
    )
    row = cur.fetchone()
    oldest = row[0] if row and row[0] else None

    print(f"  Channel: {channel_name} ({channel_id}), oldest={oldest}")

    last_ts = oldest
    next_cursor = None

    while True:
        kwargs = {"channel": channel_id, "limit": 200}
        if oldest:
            kwargs["oldest"] = oldest
        if next_cursor:
            kwargs["cursor"] = next_cursor

        print(f"  Calling conversations_history (cursor={next_cursor})")
        try:
            response = slack.conversations_history(**kwargs)
        except Exception as e:
            raise RuntimeError(f"conversations_history timed out or failed for {channel_name}: {e}") from e
        messages = response.get("messages", [])

        for msg in messages:
            # Skip system messages and bot messages
            if msg.get("subtype") or msg.get("bot_id"):
                continue

            user_id = msg.get("user")

            # Fetch and upsert any user we haven't seen yet
            if user_id and user_id not in known_users:
                try:
                    user_info = slack.users_info(user=user_id)
                    upsert_user(cur, user_info["user"])
                    known_users.add(user_id)
                except SlackApiError as e:
                    print(f"    Could not fetch user {user_id}: {e}")

            upsert_message(cur, msg, channel_id)

            # Track latest ts seen
            if last_ts is None or float(msg["ts"]) > float(last_ts):
                last_ts = msg["ts"]

            # Fetch thread replies if this is a thread parent with replies
            if msg.get("reply_count", 0) > 0 and msg.get("thread_ts") == msg.get("ts"):
                sync_thread(cur, slack, channel_id, msg["ts"], known_users)

        # Pagination
        next_cursor = response.get("response_metadata", {}).get("next_cursor")
        if not next_cursor:
            break

    # Re-check all thread parents from the last 7 days so late replies are never missed.
    # This is separate from the incremental message fetch — Slack doesn't resurface old
    # parent messages when someone replies to them, so we query our own DB for thread
    # parents and re-sync their replies directly.
    cur.execute(
        """
        SELECT DISTINCT ts FROM slack_message
        WHERE channel_id = %s
          AND is_thread_reply = false
          AND thread_ts IS NOT NULL
          AND posted_at > now() - interval '7 days'
        """,
        (channel_id,)
    )
    thread_parents = [row[0] for row in cur.fetchall()]
    if thread_parents:
        print(f"  Re-checking {len(thread_parents)} thread(s) from last 7 days")
        for parent_ts in thread_parents:
            sync_thread(cur, slack, channel_id, parent_ts, known_users)

    update_sync_state(cur, channel_id, last_ts, status="ok")
    print(f"  Done: last_ts={last_ts}")


def sync_thread(cur, slack: WebClient, channel_id: str, thread_ts: str, known_users: set) -> None:
    """Fetch and upsert all replies in a thread."""
    try:
        response = slack.conversations_replies(
            channel=channel_id,
            ts=thread_ts,
            limit=200,
        )
        # Skip index 0 — that's the parent message, already upserted
        for reply in response.get("messages", [])[1:]:
            user_id = reply.get("user")
            if user_id and user_id not in known_users:
                try:
                    user_info = slack.users_info(user=user_id)
                    upsert_user(cur, user_info["user"])
                    known_users.add(user_id)
                except SlackApiError as e:
                    print(f"    Could not fetch user {user_id}: {e}")
            upsert_message(cur, reply, channel_id)
    except SlackApiError as e:
        print(f"    Could not fetch thread {thread_ts}: {e}")


# --- Lambda entry point ---

def lambda_handler(event, context):
    print("Slack sync starting")

    slack = get_slack_client()
    conn = get_db_connection()

    try:
        cur = conn.cursor()
        known_users: set = set()

        # Get all active channels
        cur.execute("SELECT channel_id, name FROM slack_channel WHERE active = true")
        channels = cur.fetchall()
        print(f"Active channels: {len(channels)}")

        for channel_id, channel_name in channels:
            try:
                sync_channel(cur, slack, channel_id, channel_name, known_users)
                conn.commit()
            except Exception as e:
                print(f"  ERROR syncing {channel_name}: {e}")
                conn.rollback()
                # Record the error in sync state so we know which channel failed
                cur2 = conn.cursor()
                update_sync_state(cur2, channel_id, None, status="error", error_message=str(e))
                conn.commit()
                cur2.close()

        cur.close()
        print("Slack sync complete")
        return {"statusCode": 200, "body": "Sync complete"}

    finally:
        conn.close()
