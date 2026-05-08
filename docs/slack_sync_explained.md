# Slack Sync — How It Actually Works

A plain-English walkthrough of the entire system. Read this if you need to debug it, rebuild it, or explain it to someone else.

---

## The big picture

Every hour, EventBridge fires the Lambda. The Lambda:
1. Connects to Slack and to Postgres
2. Looks up which channels it should sync (from the database)
3. For each channel, asks Slack: "give me all messages since the last time I checked"
4. Saves those messages (and the users who sent them) to Postgres
5. Records the timestamp of the newest message it saw, so next run it knows where to start

That's it. The whole thing is one file: `src/sync/slack/handler.py`.

---

## The database tables

Four tables power this. Created by `db/migrations/002_slack_schema.sql`.

### `slack_channel`
Your list of channels to sync. You manage this manually via `scripts/manage_channels.py`. The Lambda reads it on every run — add a channel here and it starts syncing next hour, deactivate one and it stops.

Key columns:
- `channel_id` — Slack's internal ID (e.g. `C0AM3SP0BGE`), not the human-readable name
- `active` — boolean. Only `active = true` channels get synced
- `indexing_allowed` — controls whether the AI layer can read messages from this channel later
- `sensitivity` — `standard`, `hr_only`, or `exec_only`. The `ai_readable_messages` view filters out anything not `standard`

### `slack_user`
Every person who has sent a message in a synced channel. The Lambda populates this automatically the first time it sees a user.

Key columns:
- `is_internal` — auto-classified: does their email end in `@gymlaunch.com`, `@gymlaunchsecrets.com`, or `@gymowners.com`? This is how you'll eventually split "our team" from "clients"
- `classification_locked` — if you set this to `true` in the database, the sync will **never overwrite** `is_internal` for that user. Use this for edge cases (contractors with Gmail addresses, consultants, etc.)
- `classification_note` — a free text field to explain why you locked someone (e.g. "contractor hired 2025-03, uses gmail")
- `is_bot` — automatically flagged; bots are stored but filtered out of message processing

### `slack_message`
Every message. The core of the data lake.

Key columns:
- `ts` — Slack's timestamp format, looks like `"1609459200.000100"`. It's a Unix timestamp as a string with microseconds. This is Slack's primary key for messages — it's unique within a channel
- `thread_ts` — if this message is a reply in a thread, this is the `ts` of the parent message. If it's a top-level message, this is null
- `is_thread_reply` — boolean. True if this is a reply, false if it's a top-level message
- `raw_payload` — the full JSON blob Slack sent back. Anything not broken out into its own column is preserved here for later use
- The unique constraint is `(channel_id, ts)` — the same message can never be inserted twice

### `slack_sync_state`
One row per channel. Tracks where the sync left off.

Key columns:
- `last_ts` — the `ts` of the most recent message synced. On the next run, the Lambda passes this to Slack as the `oldest` parameter, so Slack only returns messages newer than this
- `status` — `ok`, `error`, or `never_run`. If a channel fails, this is set to `error` with the error message stored alongside it
- `last_synced_at` — wall clock time of the last successful sync. Useful for spotting if a channel has silently stopped syncing

---

## The code, function by function

### `lambda_handler` — the entry point

This is what EventBridge calls. It's the conductor.

```python
def lambda_handler(event, context):
    slack = get_slack_client()
    conn = get_db_connection()

    cur = conn.cursor()
    known_users = set()

    cur.execute("SELECT channel_id, name FROM slack_channel WHERE active = true")
    channels = cur.fetchall()

    for channel_id, channel_name in channels:
        try:
            sync_channel(cur, slack, channel_id, channel_name, known_users)
            conn.commit()
        except Exception as e:
            conn.rollback()
            update_sync_state(cur2, channel_id, None, status="error", error_message=str(e))
            conn.commit()
```

**What to notice:**
- `known_users` is a Python `set` — a list of user IDs we've already looked up this run. It's shared across all channels so we don't hit the Slack API for the same person twice in one invocation
- Each channel is committed independently. If channel B fails, channel A's data is already saved. The Lambda doesn't die — it logs the error, saves it to `slack_sync_state`, and moves to the next channel
- The `conn.rollback()` before recording the error is important: if channel B threw halfway through writing messages, we roll back that partial write before recording the error state

---

### `get_db_connection` — connecting to Postgres

```python
def get_db_connection():
    ctx = ssl.create_default_context()
    return pg8000.connect(
        host=os.environ["DB_HOST"],
        ...
        ssl_context=ctx,
        timeout=10,
    )
```

**Why pg8000 and not psycopg2:**
Psycopg2 is a C extension — it has to be compiled for the exact OS it runs on. Lambda runs on Amazon Linux. If you install psycopg2 on your Mac or this server and zip it up, the compiled binary is for the wrong OS and it crashes at import time. `pg8000` is pure Python — no compiled binary, works anywhere.

**Why SSL:**
RDS requires SSL connections. `ssl.create_default_context()` tells Python to verify the server's certificate using the system CA bundle (Amazon Linux has AWS's CA cert built in).

**Credentials:**
All connection details come from environment variables (`os.environ`). These are set at deploy time by CloudFormation, sourced from Secrets Manager via `deploy.sh`. The Lambda never calls Secrets Manager itself — the credentials are just env vars by the time it runs.

---

### `get_slack_client` — connecting to Slack

```python
def get_slack_client():
    return WebClient(token=os.environ["SLACK_BOT_TOKEN"], timeout=30)
```

The `timeout=30` is intentional. Without it, the Slack SDK defaults to no timeout. If Slack's API hangs (it occasionally does), the Lambda would sit there for the full 15-minute Lambda timeout, burning money and blocking the next scheduled run.

---

### `sync_channel` — the core loop

This is where the real work happens.

**Step 1: Find where we left off**
```python
cur.execute("SELECT last_ts FROM slack_sync_state WHERE channel_id = %s", (channel_id,))
row = cur.fetchone()
oldest = row[0] if row and row[0] else None
```
If we've synced this channel before, `oldest` will be something like `"1746000000.000100"`. If it's the first run, `oldest` is `None` and we fetch everything.

**Step 2: Fetch messages from Slack**
```python
kwargs = {"channel": channel_id, "limit": 200}
if oldest:
    kwargs["oldest"] = oldest
if next_cursor:
    kwargs["cursor"] = next_cursor

response = slack.conversations_history(**kwargs)
```

Slack's `conversations_history` API returns messages in batches of up to 200. If there are more than 200 messages since last sync, the response includes a `next_cursor` value. We loop, passing that cursor each time, until there's no cursor left (meaning we've got everything).

**Step 3: Process each message**
```python
for msg in messages:
    if msg.get("subtype") or msg.get("bot_id"):
        continue
```

Slack uses `subtype` for system events: someone joining a channel, a channel being renamed, a file being shared, etc. We skip all of these — we only want human messages. We also skip bot messages (`bot_id` present).

**Step 4: Lazy-load users**
```python
if user_id and user_id not in known_users:
    user_info = slack.users_info(user=user_id)
    upsert_user(cur, user_info["user"])
    known_users.add(user_id)
```

We don't pre-load all users. Instead, the first time we see a user ID in a message, we call `users_info` to get their name, email, etc. We add them to `known_users` so we don't look them up again. Across hundreds of messages, most will be from the same handful of people, so we typically only make a few `users_info` calls per channel.

**Step 5: Save the message**
```python
upsert_message(cur, msg, channel_id)
```

See below.

**Step 6: Check for thread replies**
```python
if msg.get("reply_count", 0) > 0 and msg.get("thread_ts") == msg.get("ts"):
    sync_thread(cur, slack, channel_id, msg["ts"], known_users)
```

`conversations_history` only returns top-level messages. If a message has replies (`reply_count > 0`) and it's the parent of those replies (`thread_ts == ts`), we call `sync_thread` to fetch the replies separately.

**Step 7: Track the newest timestamp**
```python
if last_ts is None or float(msg["ts"]) > float(last_ts):
    last_ts = msg["ts"]
```

We convert the Slack `ts` string to a float for comparison (it's a Unix timestamp). We track the largest one we've seen. At the end of the channel sync, this gets saved to `slack_sync_state` as the starting point for next run.

---

### `sync_thread` — fetching replies

```python
response = slack.conversations_replies(channel=channel_id, ts=thread_ts, limit=200)
for reply in response.get("messages", [])[1:]:
    ...
    upsert_message(cur, reply, channel_id)
```

`conversations_replies` returns the parent message at index 0, then the replies. We skip index 0 with `[1:]` because we already saved the parent message in the main loop. Everything else gets upserted the same way.

**Note on pagination:** This implementation fetches the first 200 replies per thread. If you have threads longer than 200 replies, it silently stops at 200. For a business Slack this is almost never a problem, but it's worth knowing.

---

### `upsert_message` — saving a message

"Upsert" means: insert if it doesn't exist, update if it does. This is what makes re-runs safe.

```python
INSERT INTO slack_message (...)
VALUES (...)
ON CONFLICT (channel_id, ts) DO UPDATE SET
    text        = EXCLUDED.text,
    raw_payload = EXCLUDED.raw_payload
```

If a message with the same `(channel_id, ts)` already exists, we update `text` and `raw_payload` only. This handles Slack message edits — if someone edits a message, the next sync will update the stored text. We don't update `created_at` (preserves the original ingestion time) or `posted_at` (the message was sent when it was sent).

**What `posted_at` is:**
```python
posted_at = datetime.fromtimestamp(float(ts), tz=timezone.utc)
```
Slack stores timestamps as Unix epoch strings. We convert to a proper timezone-aware datetime for Postgres. This makes querying by date range easy.

---

### `upsert_user` — saving a user

```python
INSERT INTO slack_user (user_id, name, display_name, email, is_internal, is_bot, updated_at)
VALUES (...)
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
```

The `CASE` block is the `classification_locked` guard. In plain English: "if someone has manually locked this user's classification, don't overwrite it — keep whatever is in the database. Otherwise, recalculate based on their email."

**How `is_internal` is determined:**
```python
INTERNAL_DOMAINS = ["gymlaunch.com", "gymlaunchsecrets.com", "gymowners.com"]

def is_internal_email(email):
    return any(email.lower().endswith(f"@{domain}") for domain in INTERNAL_DOMAINS)
```

If the email ends with any of your company domains, `is_internal = true`. Everyone else (clients, vendors, external contacts) gets `is_internal = false`. This is the foundation for later analytics — filtering "what are clients saying" vs "what is our team saying."

**To add a new internal domain:** Edit `INTERNAL_DOMAINS` in `handler.py` and redeploy. The next sync will re-evaluate all new users. Existing locked users are unaffected.

---

### `update_sync_state` — recording where we stopped

```python
INSERT INTO slack_sync_state (channel_id, last_ts, last_synced_at, status, error_message, updated_at)
VALUES (...)
ON CONFLICT (channel_id) DO UPDATE SET
    last_ts        = EXCLUDED.last_ts,
    last_synced_at = now(),
    status         = EXCLUDED.status,
    error_message  = EXCLUDED.error_message,
    updated_at     = now()
```

Called at the end of every channel sync (success or failure). On success: `last_ts` advances, `status = 'ok'`, `error_message = null`. On failure: `last_ts = null` (we don't advance — next run will retry from the same point), `status = 'error'`, `error_message` gets the exception text.

**Why not advance `last_ts` on error:** If we saved 50 messages then crashed, we rolled back those 50 messages. If we advanced `last_ts`, we'd skip them forever. By keeping `last_ts` where it was, the next run re-fetches from the same point and picks up where we left off.

---

## What happens on the very first run

1. `slack_sync_state` has no row for this channel yet → `oldest = None`
2. Slack returns all messages in the channel, oldest first, 200 at a time
3. For a busy channel this could be hundreds of API pages — the loop handles it
4. At the end, `last_ts` is set to the most recent message's timestamp
5. Next run, Slack only returns messages newer than that

---

## What happens if the Lambda crashes mid-channel

- Everything for that channel is rolled back (the `conn.rollback()` in `lambda_handler`)
- `slack_sync_state` for that channel is updated with `status = 'error'` and the exception message
- Other channels that already committed are unaffected
- Next run starts from the same `last_ts` as before the crash — no data is lost or skipped

---

## Common things you might need to change

**Add a new internal domain:**
Edit `INTERNAL_DOMAINS` in `handler.py`. Redeploy with `bash scripts/deploy.sh`.

**Change how often it runs:**
Edit `Schedule: rate(1 hour)` in `infra/template.yaml`. Redeploy.

**Add a new channel to sync:**
```bash
python3 scripts/manage_channels.py add <channel-name>
```
No redeploy needed — the Lambda reads from the database.

**Re-sync a channel from scratch:**
```sql
UPDATE slack_sync_state SET last_ts = NULL WHERE channel_id = 'C0AM3SP0BGE';
```
Next run will pull all history again. Messages already in the database will be upserted (not duplicated).

**See what failed:**
```sql
SELECT channel_id, status, error_message, last_synced_at
FROM slack_sync_state
WHERE status = 'error';
```

**See the most recent messages:**
```sql
SELECT u.name, m.text, m.posted_at, c.name AS channel
FROM slack_message m
JOIN slack_channel c ON c.channel_id = m.channel_id
LEFT JOIN slack_user u ON u.user_id = m.user_id
ORDER BY m.posted_at DESC
LIMIT 20;
```

---

## The view: `ai_readable_messages`

Defined in the migration. It joins messages, channels, and users — but filters out:
- Any channel where `indexing_allowed = false`
- Any channel where `sensitivity != 'standard'`

This is the layer you'll expose to the AI/RAG system later. Sensitive channels (HR conversations, exec-only channels) never appear in it, even if they're being synced and stored.

---

## What the Lambda cannot do (by design)

The Lambda's IAM role is capped by a permissions boundary (`gymlaunch-lambda-boundary`). It can:
- Write logs to CloudWatch
- Get secrets from Secrets Manager (but not update or delete them)
- Write to S3 buckets named `gymlaunch-*`
- Create/delete network interfaces (needed to attach to the VPC)

It cannot:
- Touch RDS via the AWS API (no `rds:*` actions)
- Modify IAM
- Delete S3 objects
- Rotate or delete secrets

RDS access is purely via the Postgres TCP connection — the Lambda is just a Postgres client. The IAM boundary exists to limit what damage a compromised Lambda could do to your AWS account.
