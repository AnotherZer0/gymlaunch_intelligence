"""
manage_channels.py — Add, list, and deactivate Slack channels tracked by the sync Lambda.

USAGE
-----
  python scripts/manage_channels.py list
      Show all channels in the database with their sync status.

  python scripts/manage_channels.py add <channel-name-or-id>
      Add a channel to the sync list. The bot must already be invited to the
      channel in Slack before running this command (/invite @<bot-name>).
      Accepts either a channel name (e.g. "general") or a Slack channel ID
      (e.g. "C0123456789"). If the channel is already in the database but
      inactive, it will be reactivated.

  python scripts/manage_channels.py deactivate <channel-name-or-id>
      Stop syncing a channel. Sets active=false — data already synced is
      preserved. The channel can be reactivated with the add command.

PREREQUISITES
-------------
  - AWS credentials on this machine must have access to:
      gymlaunch/db/gls_writer   (Secrets Manager)
      gymlaunch/slack/bot_token (Secrets Manager)
  - pip install slack-sdk psycopg2-binary boto3 (or use the Lambda venv)

RUN FROM
--------
  Project root: python scripts/manage_channels.py <command>
"""

import json
import os
import sys

import boto3
import psycopg2
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError


# --- Config ---

DB_HOST = "gls.cdrq9b1h5qzb.us-east-1.rds.amazonaws.com"
DB_NAME = "gymlaunch_intelligence"
AWS_REGION = "us-east-1"


# --- Credentials ---

def get_secret(secret_id: str) -> dict:
    client = boto3.client("secretsmanager", region_name=AWS_REGION)
    response = client.get_secret_value(SecretId=secret_id)
    return json.loads(response["SecretString"])


def get_db_connection():
    secret = get_secret("gymlaunch/db/gls_writer")
    return psycopg2.connect(
        host=DB_HOST,
        port=5432,
        dbname=DB_NAME,
        user="gls_writer",
        password=secret["gls_writer"],
        sslmode="require",
        connect_timeout=10,
    )


def get_slack_client() -> WebClient:
    secret = get_secret("gymlaunch/slack/bot_token")
    return WebClient(token=secret["bot_token"])


# --- Slack helpers ---

def resolve_channel(slack: WebClient, name_or_id: str) -> dict:
    """
    Return a Slack channel dict for the given name or ID.
    Raises SystemExit if not found or bot is not a member.
    """
    # If it looks like an ID already, fetch directly
    if name_or_id.startswith("C") and name_or_id.isupper():
        try:
            info = slack.conversations_info(channel=name_or_id)
            return info["channel"]
        except SlackApiError as e:
            print(f"Error: could not fetch channel {name_or_id}: {e.response['error']}")
            sys.exit(1)

    # Otherwise search by name
    name = name_or_id.lstrip("#").lower()
    cursor = None
    while True:
        kwargs = {"limit": 200, "types": "public_channel,private_channel"}
        if cursor:
            kwargs["cursor"] = cursor
        try:
            response = slack.conversations_list(**kwargs)
        except SlackApiError as e:
            print(f"Error listing channels: {e.response['error']}")
            sys.exit(1)

        for ch in response.get("channels", []):
            if ch["name"].lower() == name:
                return ch

        cursor = response.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break

    print(f"Error: channel '{name_or_id}' not found. Make sure the bot is invited and the name is correct.")
    sys.exit(1)


# --- Commands ---

def cmd_list():
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT
                c.channel_id,
                c.name,
                c.active,
                s.last_synced_at,
                s.status,
                COUNT(m.id) AS message_count
            FROM slack_channel c
            LEFT JOIN slack_sync_state s ON s.channel_id = c.channel_id
            LEFT JOIN slack_message m ON m.channel_id = c.channel_id
            GROUP BY c.channel_id, c.name, c.active, s.last_synced_at, s.status
            ORDER BY c.name
        """)
        rows = cur.fetchall()
        if not rows:
            print("No channels in database.")
            return
        print(f"\n{'ID':<14} {'Name':<30} {'Active':<8} {'Last Synced':<22} {'Status':<10} {'Messages'}")
        print("-" * 95)
        for row in rows:
            ch_id, name, active, last_synced, status, msg_count = row
            print(f"{ch_id:<14} {name:<30} {str(active):<8} {str(last_synced or 'never'):<22} {str(status or '-'):<10} {msg_count}")
        print()
    finally:
        conn.close()


def cmd_add(name_or_id: str):
    slack = get_slack_client()
    channel = resolve_channel(slack, name_or_id)

    ch_id = channel["id"]
    ch_name = channel["name"]
    is_member = channel.get("is_member", False)
    num_members = channel.get("num_members", "?")

    print(f"\nFound: #{ch_name} ({ch_id}), {num_members} members")

    if not is_member:
        print(f"\nWarning: the bot is not a member of #{ch_name}.")
        print(f"  Run /invite @<bot-name> in #{ch_name} first, then re-run this command.")
        sys.exit(1)

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
        print(f"Added #{ch_name} ({ch_id}) — will be synced on the next Lambda run.")
    finally:
        conn.close()


def cmd_deactivate(name_or_id: str):
    slack = get_slack_client()
    channel = resolve_channel(slack, name_or_id)

    ch_id = channel["id"]
    ch_name = channel["name"]

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE slack_channel SET active = false WHERE channel_id = %s",
            (ch_id,),
        )
        if cur.rowcount == 0:
            print(f"#{ch_name} ({ch_id}) is not in the database.")
        else:
            conn.commit()
            print(f"Deactivated #{ch_name} ({ch_id}) — existing data preserved, sync stopped.")
    finally:
        conn.close()


# --- Entry point ---

COMMANDS = {"list": cmd_list, "add": cmd_add, "deactivate": cmd_deactivate}

if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or args[0] not in COMMANDS:
        print("Usage:")
        print("  python scripts/manage_channels.py list")
        print("  python scripts/manage_channels.py add <channel-name-or-id>")
        print("  python scripts/manage_channels.py deactivate <channel-name-or-id>")
        sys.exit(1)

    cmd = args[0]
    if cmd == "list":
        cmd_list()
    elif len(args) < 2:
        print(f"Error: '{cmd}' requires a channel name or ID.")
        sys.exit(1)
    else:
        COMMANDS[cmd](args[1])
