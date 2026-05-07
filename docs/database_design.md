# Database Design

## Philosophy

Three rules that drive every decision here:

1. **One place per fact.** A client exists once in `client_account`. Their HubSpot ID, SubscriptionFlow ID, and Slack channel ID all point back to that one row. No duplication across systems.
2. **Raw data in, derived data separate.** We store what actually happened (Slack messages, billing events) in plain tables. Summaries, insights, and AI outputs live in separate tables that reference the raw data. This means we can re-run analysis without losing history.
3. **The AI proposes, humans approve.** Nothing the AI produces writes directly to external systems. It goes to a staging layer first.

---

## Schema overview

```
organization
    └── client_account          ← one row per client/gym
            └── client_external_id   ← maps client to IDs in every external tool
            └── client_contact       ← people at the client (signer, scheduler, etc.)
            └── slack_channels       ← Slack channels associated with this client
```

---

## Migration 001 — Foundation (identity layer)

**File:** `db/migrations/001_foundation.sql`

### `organization`
Your two business entities. Everything is scoped to one of these.

| Column | Type | Notes |
|--------|------|-------|
| id | BIGSERIAL | Primary key |
| name | TEXT | e.g. "Gym Launch Secrets", "Prestige Labs" |
| created_at | TIMESTAMPTZ | |

Seed rows to insert after migration:
```sql
INSERT INTO organization (name) VALUES ('Gym Launch Secrets');
INSERT INTO organization (name) VALUES ('Prestige Labs');
```

### `client_account`
One row per client. This is the canonical record — every other system's data links back here.

| Column | Type | Notes |
|--------|------|-------|
| id | BIGSERIAL | Primary key (internal only — vendors never see this) |
| organization_id | BIGINT | FK → organization. Which LLC this client belongs to |
| name | TEXT | Human-readable name e.g. "Bob's Gym" |
| created_at | TIMESTAMPTZ | |
| updated_at | TIMESTAMPTZ | |

### `client_external_id`
Maps a `client_account` to IDs in external systems. One row per (client, system, id type).

| Column | Type | Notes |
|--------|------|-------|
| id | BIGSERIAL | Primary key |
| client_account_id | BIGINT | FK → client_account |
| system | TEXT | Lowercase snake_case: `hubspot`, `subscriptionflow`, `slack`, etc. |
| id_type | TEXT | What kind of ID: `company`, `contact`, `channel`, `customer`, etc. |
| value | TEXT | The actual ID string from that system |
| created_at | TIMESTAMPTZ | |

**Unique constraint:** `(system, id_type, value)` — the same external ID cannot point to two different clients.

**Example rows for Bob's Gym:**
```
system='hubspot',          id_type='company',  value='12345678'
system='subscriptionflow', id_type='customer', value='sf_abc123'
system='slack',            id_type='channel',  value='C0B23MKFPG8'
```

**Lookup pattern:** "Which client is HubSpot company 12345678?"
```sql
SELECT ca.*
FROM client_account ca
JOIN client_external_id cei ON cei.client_account_id = ca.id
WHERE cei.system = 'hubspot'
  AND cei.id_type = 'company'
  AND cei.value = '12345678';
```

---

## Migration 002 — Slack schema

**File:** `db/migrations/002_slack_schema.sql`

### `slack_user`
Every person in your Slack workspace — internal team and external clients.

| Column | Type | Notes |
|--------|------|-------|
| user_id | TEXT | Primary key — Slack user ID e.g. `U01234ABC` |
| name | TEXT | Full name |
| display_name | TEXT | Slack display name |
| email | TEXT | Used to auto-classify internal vs client |
| is_internal | BOOLEAN | True if email matches internal domains (gymlaunch.com etc.) |
| classification_locked | BOOLEAN | If true, sync never overwrites `is_internal` |
| classification_note | TEXT | Free text — why this user was manually classified e.g. "contractor, hired 2025-03" |
| is_bot | BOOLEAN | True for bot users |

**Classification logic in the sync Lambda:**
- If `classification_locked = true` → skip, never update `is_internal`
- Otherwise → set `is_internal = true` if email domain is in the internal domain list

**Internal domains:** `gymlaunch.com`, `gymlaunchsecrets.com`, `gymowners.com`

### `slack_channel`
Every Slack channel we monitor, with client mapping and access controls.

| Column | Type | Notes |
|--------|------|-------|
| channel_id | TEXT | Primary key — Slack channel ID e.g. `C01234ABC` |
| name | TEXT | Channel name e.g. `gl-bobs-gym-support` |
| channel_type | TEXT | `private_client` \| `public_internal` \| `public_client` |
| client_account_id | BIGINT | FK → client_account. Which client this channel belongs to |
| sensitivity | TEXT | `standard` \| `hr_only` \| `exec_only` — controls AI visibility |
| indexing_allowed | BOOLEAN | If false, messages never appear in AI queries |
| active | BOOLEAN | Set to false to pause tracking without deleting |

### `slack_message`
Raw messages exactly as they came from Slack.

| Column | Type | Notes |
|--------|------|-------|
| id | BIGSERIAL | Surrogate primary key |
| channel_id | TEXT | FK → slack_channel |
| user_id | TEXT | FK → slack_user (null for bots) |
| ts | TEXT | Slack timestamp e.g. `1609459200.000100` — unique per channel |
| thread_ts | TEXT | Parent message ts. Same as ts if root message, different if reply |
| is_thread_reply | BOOLEAN | True if this message is a reply inside a thread |
| text | TEXT | Message content |
| posted_at | TIMESTAMPTZ | ts converted to real timestamp for easy date filtering |
| raw_payload | JSONB | Full Slack API response — reactions, attachments, edits, etc. |

**Unique constraint:** `(channel_id, ts)` — Slack guarantees this is unique per workspace. Used for idempotent upserts (safe to re-run sync without duplicates).

**Accessing reactions from raw_payload:**
```sql
-- Messages with a :ticket: reaction
SELECT * FROM slack_message
WHERE raw_payload @> '{"reactions": [{"name": "ticket"}]}';
```

### `slack_sync_state`
One row per channel. Tracks where the sync Lambda left off.

| Column | Type | Notes |
|--------|------|-------|
| channel_id | TEXT | Primary key, FK → slack_channel |
| last_ts | TEXT | Last Slack ts successfully synced |
| last_synced_at | TIMESTAMPTZ | Wall clock time of last successful run |
| status | TEXT | `ok` \| `error` \| `never_run` |
| error_message | TEXT | What went wrong if status = `error` |

### `ai_readable_messages` (view)
What AI queries and read-only consumers see. Filters out restricted channels automatically.

```sql
-- Filters: indexing_allowed = true AND sensitivity = 'standard'
-- Joins in channel and user info for convenience
SELECT * FROM ai_readable_messages
WHERE client_account_id = 42
  AND posted_at > now() - interval '7 days';
```

To restrict a channel from AI visibility — no code change needed, just update the data:
```sql
-- Exclude a channel from AI entirely
UPDATE slack_channel SET indexing_allowed = false WHERE channel_id = 'C01234ABC';

-- Mark a channel as exec-only
UPDATE slack_channel SET sensitivity = 'exec_only' WHERE channel_id = 'C01234ABC';
```

---

## Migration 003 — SubscriptionFlow (planned)

Tables: `sf_customer`, `sf_subscription`
Both include a `raw_payload JSONB` column for the full API response.

---

## Users and permissions

Three database users:

| User | Access | Used by |
|------|--------|---------|
| `glsadmin` | Full (owner) | Migrations only |
| `gls_writer` | INSERT, UPDATE, SELECT | Lambda sync functions |
| `gls_reader` | SELECT only | n8n, reporting tools, developer queries |

---

## Naming conventions

- Tables: `snake_case`, plural avoided (e.g. `client_account` not `client_accounts`)
- `system` values in `client_external_id`: always lowercase snake_case (`hubspot`, `zoho_subscriptions`, `subscriptionflow`, `intacct`, `stripe`, `slack`, `asana`, `gohighlevel`)
- Timestamps: always `TIMESTAMPTZ` (timezone-aware), never bare `TIMESTAMP`
- Foreign keys: always named `{table}_id` e.g. `client_account_id`
- Soft deletes preferred over hard deletes where data has dependencies
