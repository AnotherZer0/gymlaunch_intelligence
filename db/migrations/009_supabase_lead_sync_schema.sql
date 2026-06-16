-- Mirror tables for the daily Supabase → RDS sync (gymlaunch-supabase-lead-sync).
-- Source: Supabase project lvidkcbzjvzuxwhtmndq, public schema. 7 tables.
--
-- The Supabase "00 - Lead Integration Main Database" table is intentionally NOT
-- mirrored — the same per-client metadata is already captured in
-- asana_agency_board_task (FB page IDs, GHL location ID, gym/client name).
--
-- Phase 1 = mirror + per-table sync state. Phase 2 (compare view + Google Sheet
-- output + integration-break threshold) is deferred until business decides on
-- the threshold and the system-user/non-system-user distinction.
--
-- Depends on: 001_foundation.sql

BEGIN;

-- =========================================================================
-- Lead tables — the two that get compared in phase 2
-- =========================================================================

-- Mirror of Supabase "02 - Facebook Leads".
-- Source `created_at` is M/D/YYYY *text* (not a real timestamp). Parsed to DATE
-- on the way in as `lead_date`; original text kept in `created_at_raw` for
-- forensics. All non-PK columns NULLABLE so an upstream schema change doesn't
-- break the sync — better to land NULL than to drop the row.
CREATE TABLE IF NOT EXISTS supabase_facebook_lead (
    lead_id              TEXT        PRIMARY KEY,
    page_id              TEXT,
    page_name            TEXT,
    source               TEXT,                -- always 'fb' in practice
    sub_account_id       TEXT,                -- = GHL location ID, join key
    sub_account_name     TEXT,
    form_id              TEXT,
    form_name            TEXT,
    first_name           TEXT,
    last_name            TEXT,
    email                TEXT,
    phone                TEXT,
    execution_id_link    TEXT,
    lead_date            DATE,                -- parsed from source created_at (M/D/YYYY)
    created_at_raw       TEXT,                -- original Supabase value
    synced_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS supabase_facebook_lead_subacct_date_idx
    ON supabase_facebook_lead (sub_account_id, lead_date);

CREATE INDEX IF NOT EXISTS supabase_facebook_lead_date_idx
    ON supabase_facebook_lead (lead_date);


-- Mirror of Supabase "03 - HighLevel Leads". Identical schema to facebook_lead.
-- Source values for `source` are 'hlold' or 'hlnew' — both count as a GHL lead
-- in the (deferred) phase-2 comparison view.
CREATE TABLE IF NOT EXISTS supabase_highlevel_lead (
    lead_id              TEXT        PRIMARY KEY,
    page_id              TEXT,
    page_name            TEXT,
    source               TEXT,                -- 'hlold' | 'hlnew'
    sub_account_id       TEXT,                -- = GHL location ID, join key
    sub_account_name     TEXT,
    form_id              TEXT,
    form_name            TEXT,
    first_name           TEXT,
    last_name            TEXT,
    email                TEXT,
    phone                TEXT,
    execution_id_link    TEXT,
    lead_date            DATE,                -- parsed from source created_at (M/D/YYYY)
    created_at_raw       TEXT,
    synced_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS supabase_highlevel_lead_subacct_date_idx
    ON supabase_highlevel_lead (sub_account_id, lead_date);

CREATE INDEX IF NOT EXISTS supabase_highlevel_lead_date_idx
    ON supabase_highlevel_lead (lead_date);


-- =========================================================================
-- Form / reference tables — small, slowly changing
-- =========================================================================

-- Mirror of Supabase "01 - Lead Form Database".
-- Date columns kept as TEXT (raw) — they're inconsistent in source and we don't
-- currently need them parsed. Promote to DATE if a downstream consumer needs it.
CREATE TABLE IF NOT EXISTS supabase_lead_form (
    form_id              TEXT        PRIMARY KEY,
    form_name            TEXT,
    facebook_page_id     TEXT,
    facebook_page_name   TEXT,
    create_date_raw      TEXT,
    date_added_raw       TEXT,
    synced_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);


-- Mirror of Supabase "lead_forms" — the richer form-detail table (questions,
-- thank-you page config, leads_count snapshot). JSONB columns are stored as
-- JSONB in RDS so they're queryable without re-parsing.
CREATE TABLE IF NOT EXISTS supabase_lead_form_detail (
    form_id                TEXT        PRIMARY KEY,
    page_id                TEXT,
    client_name            TEXT,
    name                   TEXT,
    status                 TEXT,
    locale                 TEXT,
    created_time           TIMESTAMPTZ,
    leads_count            INTEGER,
    questions              JSONB,
    context_card           JSONB,
    thank_you_page         JSONB,
    follow_up_action_url   TEXT,
    raw                    JSONB,
    last_synced_at_source  TIMESTAMPTZ,         -- Supabase's own last_synced_at
    synced_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);


-- =========================================================================
-- Legacy tables — kept for archive / debugging, may not be actively written
-- =========================================================================

-- Mirror of legacy Supabase "Facebook Form Database".
-- Note: source PK is facebook_page_id alone (one row per page, not per form) —
-- mirrored as-is even though it's logically suspicious.
CREATE TABLE IF NOT EXISTS supabase_facebook_form (
    facebook_page_id     TEXT        PRIMARY KEY,
    facebook_page_name   TEXT,
    form_id              TEXT,
    form_name            TEXT,
    create_date          DATE,
    date_added           DATE,
    is_active            BOOLEAN,
    synced_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);


-- Mirror of legacy Supabase "Facebook Lead Form Database". Near-empty in
-- practice — schema is essentially just an autoincrement id + a timestamp.
CREATE TABLE IF NOT EXISTS supabase_facebook_lead_form (
    id              BIGINT      PRIMARY KEY,
    created_at      TIMESTAMPTZ,
    synced_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);


-- Mirror of legacy Supabase "Facebook Leads Database" — predecessor of
-- "02 - Facebook Leads". Different column names but conceptually the same data.
CREATE TABLE IF NOT EXISTS supabase_facebook_lead_legacy (
    facebook_leadgen_id        TEXT        PRIMARY KEY,
    facebook_page_id           TEXT,
    facebook_form_id           TEXT,
    facebook_lead_first_name   TEXT,
    facebook_lead_last_name    TEXT,
    facebook_lead_email        TEXT,
    facebook_lead_phone        TEXT,
    system_execution_id_url    TEXT,
    system_status              TEXT,
    system_created_at          DATE,
    synced_at                  TIMESTAMPTZ NOT NULL DEFAULT now()
);


-- =========================================================================
-- Sync state — one row per mirrored table
-- =========================================================================

-- Updated at the end of each per-table sync. Errors set last_status='error'
-- and populate last_error so operators can investigate without trawling logs.
CREATE TABLE IF NOT EXISTS supabase_sync_state (
    table_name           TEXT        PRIMARY KEY,    -- the RDS destination name
    last_synced_at       TIMESTAMPTZ,
    last_row_count       INTEGER,
    last_status          TEXT        NOT NULL DEFAULT 'never_run',  -- 'success' | 'error' | 'never_run'
    last_error           TEXT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMIT;
