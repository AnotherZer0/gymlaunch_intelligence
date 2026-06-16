-- Mirror of the `00 - Database` tab in the Google Sheet `Lead Integration Database V2.0`
-- (Sheet ID `1JGdbjR1g8MF0zzraOwyPNNY7-jRrVIzk2ZZI-FWhky0`).
--
-- That sheet is the operational client master for the FB-lead pipeline. It carries
-- both auto-populated fields (written by the n8n workflow `00 - Main Workflow` on
-- every FB leadgen webhook fire) and human-maintained diagnostic flags
-- (`system_user`, `fb_app_status`, `ghl_connection`, etc.) that the team uses to
-- track integration health per client. The diagnostic columns ARE the source of
-- truth for the system-user / non-system-user distinction we need in the phase-2
-- compare sheet.
--
-- This table is populated by `gymlaunch-lead_db2-sheet-sync`, which reads the
-- sheet via the Google Sheets API once a day at 07:00 UTC (one hour before
-- `gymlaunch-supabase-lead-sync`).
--
-- Excluded from the mirror: `hl_sub_account_api_key`. That column carries
-- plaintext GHL API keys per client, and we deliberately do not propagate them
-- into our RDS — same reasoning as for the Supabase `00 - Lead Integration Main
-- Database` mirror we declined to build.
--
-- Depends on: 001_foundation.sql, 009_supabase_lead_sync_schema.sql (uses the
-- existing `supabase_sync_state` table for the per-run watermark).

BEGIN;

CREATE TABLE IF NOT EXISTS client_lead_master (
    -- ===== Identity / mapping (auto-populated by n8n workflow) =====
    facebook_page_id              TEXT PRIMARY KEY,
    facebook_page_name            TEXT,
    facebook_ad_account_id        TEXT,
    facebook_ad_account_name      TEXT,
    hl_sub_account_location_id    TEXT,       -- join key to supabase_*_lead.sub_account_id
    hl_sub_account_location_name  TEXT,
    asana_task_id                 TEXT,
    gym_name                      TEXT,
    client_name                   TEXT,

    -- ===== Auto-populated status (written by n8n) =====
    asana_status                  TEXT,       -- e.g. "Active", "Paused", "Churned"
    workflow_status               TEXT,       -- "Succeeded" / error description

    -- ===== Human-maintained diagnostics (manual) =====
    ghl_connection                TEXT,       -- e.g. "Ivan's Account", "Brent's Account"
    workflow_connection           TEXT,       -- e.g. "Make" / "No"
    page_connection               TEXT,       -- e.g. "Personal - Alex Burner"
    lead_access_issue             TEXT,       -- usually "None"
    fb_app_status                 TEXT,       -- "Error" used to filter Non-System-User heatmap
    -- Sheet column is named `system_user` but that's a reserved keyword in
    -- PostgreSQL 16+ (SQL:2023, returns the authenticated session user). Mapped
    -- to `is_system_user` here; the Lambda handles the rename when ingesting.
    is_system_user                TEXT,       -- "Yes" / "No" — primary system-user flag
    ghl_snapshot                  TEXT,       -- e.g. "GGE"
    notes                         TEXT,
    lead_forms                    TEXT,       -- usually Yes/No/empty
    supabase                      TEXT,       -- "Yes" / "No" — flags clients in the new pipeline

    -- ===== Bookkeeping =====
    synced_at                     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS client_lead_master_location_idx
    ON client_lead_master (hl_sub_account_location_id);

CREATE INDEX IF NOT EXISTS client_lead_master_system_user_idx
    ON client_lead_master (is_system_user);

CREATE INDEX IF NOT EXISTS client_lead_master_asana_status_idx
    ON client_lead_master (asana_status);

COMMIT;
