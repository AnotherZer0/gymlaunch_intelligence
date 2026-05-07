-- Slack ingestion schema
-- Depends on: 001_foundation.sql (client_account must exist)

BEGIN;

-- Users in your Slack workspace (internal team + external clients)
CREATE TABLE IF NOT EXISTS slack_user (
    user_id                 TEXT PRIMARY KEY,  -- Slack user ID e.g. U01234ABC
    name                    TEXT NOT NULL,
    display_name            TEXT,
    email                   TEXT,
    is_internal             BOOLEAN NOT NULL DEFAULT false,
    classification_locked   BOOLEAN NOT NULL DEFAULT false,
    classification_note     TEXT,              -- e.g. "contractor hired 2025-03, uses gmail"
    is_bot                  BOOLEAN NOT NULL DEFAULT false,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS slack_user_email_idx
    ON slack_user (email);

CREATE INDEX IF NOT EXISTS slack_user_is_internal_idx
    ON slack_user (is_internal);

-- Slack channels with client mapping and sensitivity controls
CREATE TABLE IF NOT EXISTS slack_channel (
    channel_id          TEXT PRIMARY KEY,  -- Slack channel ID e.g. C01234ABC
    name                TEXT NOT NULL,
    channel_type        TEXT NOT NULL DEFAULT 'private_client',
                        -- 'private_client' | 'public_internal' | 'public_client'
    client_account_id   BIGINT REFERENCES client_account (id) ON DELETE SET NULL,
    sensitivity         TEXT NOT NULL DEFAULT 'standard',
                        -- 'standard' | 'hr_only' | 'exec_only'
    indexing_allowed    BOOLEAN NOT NULL DEFAULT true,
    active              BOOLEAN NOT NULL DEFAULT true,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS slack_channel_client_idx
    ON slack_channel (client_account_id);

CREATE INDEX IF NOT EXISTS slack_channel_active_idx
    ON slack_channel (active);

-- Raw Slack messages
CREATE TABLE IF NOT EXISTS slack_message (
    id              BIGSERIAL PRIMARY KEY,
    channel_id      TEXT NOT NULL REFERENCES slack_channel (channel_id) ON DELETE RESTRICT,
    user_id         TEXT REFERENCES slack_user (user_id) ON DELETE SET NULL,
    ts              TEXT NOT NULL,       -- Slack timestamp e.g. "1609459200.000100"
    thread_ts       TEXT,               -- Parent message ts; same as ts if root message
    is_thread_reply BOOLEAN NOT NULL DEFAULT false,
    text            TEXT,
    posted_at       TIMESTAMPTZ NOT NULL, -- ts converted to real timestamp
    raw_payload     JSONB,              -- Full Slack API response for anything not columned
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT slack_message_channel_ts_uniq UNIQUE (channel_id, ts)
);

CREATE INDEX IF NOT EXISTS slack_message_channel_posted_idx
    ON slack_message (channel_id, posted_at DESC);

CREATE INDEX IF NOT EXISTS slack_message_thread_idx
    ON slack_message (channel_id, thread_ts);

CREATE INDEX IF NOT EXISTS slack_message_user_idx
    ON slack_message (user_id);

CREATE INDEX IF NOT EXISTS slack_message_posted_at_idx
    ON slack_message (posted_at DESC);

-- Per-channel sync state for the reconciliation job
CREATE TABLE IF NOT EXISTS slack_sync_state (
    channel_id      TEXT PRIMARY KEY REFERENCES slack_channel (channel_id) ON DELETE CASCADE,
    last_ts         TEXT,               -- Last Slack ts successfully synced
    last_synced_at  TIMESTAMPTZ,        -- Wall clock time of last successful sync
    status          TEXT NOT NULL DEFAULT 'never_run',
                    -- 'ok' | 'error' | 'never_run'
    error_message   TEXT,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- View: what the AI and read-only consumers are allowed to see
-- Filters out channels marked as not indexable or sensitive
CREATE OR REPLACE VIEW ai_readable_messages AS
    SELECT
        m.id,
        m.channel_id,
        m.user_id,
        m.ts,
        m.thread_ts,
        m.is_thread_reply,
        m.text,
        m.posted_at,
        c.client_account_id,
        c.channel_type,
        c.name AS channel_name,
        u.is_internal,
        u.name AS user_name
    FROM slack_message m
    JOIN slack_channel c ON c.channel_id = m.channel_id
    LEFT JOIN slack_user u ON u.user_id = m.user_id
    WHERE c.indexing_allowed = true
      AND c.sensitivity = 'standard';

COMMIT;
