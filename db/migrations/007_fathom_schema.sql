-- Fathom call transcript schema
-- Receives webhook payloads (via Zapier) after each recorded call.
-- Depends on: 001_foundation.sql

BEGIN;

CREATE TABLE IF NOT EXISTS fathom_call (
    id                                    BIGSERIAL    PRIMARY KEY,
    fathom_id                             TEXT         NOT NULL UNIQUE,
    meeting_title                         TEXT,
    fathom_user_name                      TEXT,
    fathom_user_email                     TEXT,
    fathom_user_team                      TEXT,
    meeting_scheduled_start_time          TIMESTAMPTZ,
    meeting_scheduled_end_time            TIMESTAMPTZ,
    meeting_scheduled_duration_in_minutes TEXT,
    recording_duration_in_minutes         NUMERIC,
    recording_url                         TEXT,
    recording_share_url                   TEXT,
    meeting_join_url                      TEXT,
    transcript_plaintext                  TEXT,
    summary                               TEXT,        -- AI-generated summary, filled separately
    raw_payload                           JSONB,
    received_at                           TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS fathom_call_user_email_idx
    ON fathom_call (fathom_user_email);

CREATE INDEX IF NOT EXISTS fathom_call_start_time_idx
    ON fathom_call (meeting_scheduled_start_time DESC);

-- One row per invitee per call. Indexed on email for AI brain queries.
-- Query pattern: SELECT fc.* FROM fathom_call fc
--                JOIN fathom_call_invitee fci ON fci.call_id = fc.id
--                WHERE fci.email = 'john@example.com';
CREATE TABLE IF NOT EXISTS fathom_call_invitee (
    id          BIGSERIAL   PRIMARY KEY,
    call_id     BIGINT      NOT NULL REFERENCES fathom_call (id) ON DELETE CASCADE,
    name        TEXT,
    email       TEXT,
    is_external BOOLEAN,
    domain_name TEXT,
    CONSTRAINT fathom_call_invitee_call_email_uniq UNIQUE (call_id, email)
);

CREATE INDEX IF NOT EXISTS fathom_call_invitee_email_idx
    ON fathom_call_invitee (email);

CREATE INDEX IF NOT EXISTS fathom_call_invitee_call_idx
    ON fathom_call_invitee (call_id);

COMMIT;
