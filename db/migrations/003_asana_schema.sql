-- Asana ingestion schema — Agency Board
-- Project GID: 1206006426591402
-- Depends on: 001_foundation.sql

BEGIN;

CREATE TABLE IF NOT EXISTS asana_user (
    gid         TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    email       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS asana_user_email_idx ON asana_user (email);


CREATE TABLE IF NOT EXISTS asana_project (
    gid             TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    workspace_gid   TEXT NOT NULL,
    workspace_name  TEXT NOT NULL,
    team_gid        TEXT,
    team_name       TEXT,
    archived        BOOLEAN NOT NULL DEFAULT false,
    active          BOOLEAN NOT NULL DEFAULT true,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS asana_project_active_idx ON asana_project (active);


-- All tasks from active projects. Subtasks self-reference via parent_task_gid.
-- section_name drives the MB capacity count (from memberships, not a custom field).
CREATE TABLE IF NOT EXISTS asana_task (
    gid                 TEXT PRIMARY KEY,
    project_gid         TEXT REFERENCES asana_project (gid) ON DELETE SET NULL,
    parent_task_gid     TEXT REFERENCES asana_task (gid) ON DELETE SET NULL,
    assignee_gid        TEXT REFERENCES asana_user (gid) ON DELETE SET NULL,
    name                TEXT NOT NULL,
    notes               TEXT,
    section_name        TEXT,
    due_on              DATE,
    completed           BOOLEAN NOT NULL DEFAULT false,
    completed_at        TIMESTAMPTZ,
    asana_created_at    TIMESTAMPTZ,
    asana_modified_at   TIMESTAMPTZ,
    raw_payload         JSONB,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS asana_task_project_idx      ON asana_task (project_gid);
CREATE INDEX IF NOT EXISTS asana_task_parent_idx       ON asana_task (parent_task_gid);
CREATE INDEX IF NOT EXISTS asana_task_assignee_idx     ON asana_task (assignee_gid);
CREATE INDEX IF NOT EXISTS asana_task_section_idx      ON asana_task (section_name);
CREATE INDEX IF NOT EXISTS asana_task_modified_at_idx  ON asana_task (asana_modified_at DESC);
CREATE INDEX IF NOT EXISTS asana_task_completed_idx    ON asana_task (completed);


CREATE TABLE IF NOT EXISTS asana_task_comment (
    gid              TEXT PRIMARY KEY,
    task_gid         TEXT NOT NULL REFERENCES asana_task (gid) ON DELETE CASCADE,
    author_gid       TEXT REFERENCES asana_user (gid) ON DELETE SET NULL,
    text             TEXT,
    created_at_asana TIMESTAMPTZ NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS asana_task_comment_task_idx ON asana_task_comment (task_gid);


-- Board-specific typed custom fields for the Agency Board.
-- One row per task in the project, including one-offs (gym_name IS NULL for those).
-- hl_sub_account_api_key is vestigial — never populated by the sync.
-- coach: read from Asana for now; future plan is HubSpot → Asana with
--        "Agency Pro" exception (never overwrite that value from HubSpot).
CREATE TABLE IF NOT EXISTS asana_agency_board_task (
    task_gid                    TEXT PRIMARY KEY REFERENCES asana_task (gid) ON DELETE CASCADE,

    -- Custom fields (Asana field names in comments)
    gym_name                    TEXT,           -- "Gym Name"
    client_name                 TEXT,           -- "Client Name"
    agency_status               TEXT,           -- "Agency Status"
    account_manager             TEXT,           -- "Account Manager" (dropdown, not full name)
    media_buyer                 TEXT,           -- "Media Buyer" (dropdown, not full name)
    coach                       TEXT,           -- "Coach"
    hubspot_company_id          TEXT,           -- "Hubspot Company ID"
    facebook_page_name          TEXT,           -- "FB Page Name"
    facebook_page_id            TEXT,           -- "FB Page ID"
    facebook_ad_account_id      TEXT,           -- "FB Ad Account ID"
    facebook_ad_account_name    TEXT,           -- "FB Ad Account Name"
    hl_sub_account_location_id  TEXT,           -- "GHL Location ID"
    hl_sub_account_api_key      TEXT,           -- vestigial, manually maintained, never synced
    ads_live_date               DATE,           -- "Actual Live Date"
    ad_spend_budget_daily       NUMERIC,        -- "Ad Spend Budget Daily"

    -- HubSpot sync state (replaces N8N_Hash / Last_Update_Hash pattern)
    content_hash                TEXT,           -- hash of HubSpot-relevant fields, recomputed each sync
    last_synced_hash            TEXT,           -- hash at last successful HubSpot push; NULL = never pushed
    last_hubspot_run_status     TEXT,           -- 'success' | error string
    last_synced_at              TIMESTAMPTZ,

    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS asana_agency_board_hubspot_idx
    ON asana_agency_board_task (hubspot_company_id);

CREATE INDEX IF NOT EXISTS asana_agency_board_media_buyer_idx
    ON asana_agency_board_task (media_buyer);

-- Partial index for the HubSpot sync query: only rows that need pushing
CREATE INDEX IF NOT EXISTS asana_agency_board_needs_sync_idx
    ON asana_agency_board_task (task_gid)
    WHERE hubspot_company_id IS NOT NULL
      AND (last_synced_hash IS NULL OR content_hash != last_synced_hash);


-- Per-project incremental sync cursor.
CREATE TABLE IF NOT EXISTS asana_sync_state (
    project_gid          TEXT PRIMARY KEY REFERENCES asana_project (gid) ON DELETE CASCADE,
    last_modified_since  TIMESTAMPTZ,   -- NULL triggers a full pull on the next run
    last_synced_at       TIMESTAMPTZ,
    status               TEXT NOT NULL DEFAULT 'never_run',
    error_message        TEXT,
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);


-- Replaces the Google Sheet COUNTIFS formula.
-- Counts roster clients (gym_name IS NOT NULL) per MB/AM where section = active work.
CREATE OR REPLACE VIEW mb_capacity AS
SELECT
    ab.media_buyer,
    ab.account_manager,
    COUNT(*) FILTER (
        WHERE t.section_name IN (
            'Onboarding',
            'Ready To Go Live',
            'Downed Account',
            'Active Accounts'
        )
    ) AS active_client_count,
    COUNT(*) AS total_roster_assigned
FROM asana_agency_board_task ab
JOIN asana_task t ON t.gid = ab.task_gid
WHERE t.parent_task_gid IS NULL
  AND ab.gym_name IS NOT NULL
GROUP BY ab.media_buyer, ab.account_manager;

COMMIT;
