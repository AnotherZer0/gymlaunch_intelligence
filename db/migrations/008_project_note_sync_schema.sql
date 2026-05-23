-- HubSpot project-note association sync state
-- Tracks (note, project) pairs the gymlaunch-project-note-sync Lambda has reconciled.
-- The Lambda iterates Projects (0-970) nightly and ensures each note attached to a
-- project is also associated with the project's companies and contacts. This table is
-- what lets the Lambda skip pairs whose desired state hasn't drifted since the last
-- successful sync — no time window, just state.
-- Depends on: 001_foundation.sql

BEGIN;

CREATE TABLE IF NOT EXISTS hubspot_project_note_sync (
    note_id              TEXT        NOT NULL,
    project_id           TEXT        NOT NULL,
    -- Snapshot of the project's companies/contacts as of the last successful sync.
    -- If the live values diverge on a later run, the pair is re-processed even if
    -- companies_synced_at / contacts_synced_at are already populated.
    project_company_ids  TEXT[]      NOT NULL DEFAULT ARRAY[]::TEXT[],
    project_contact_ids  TEXT[]      NOT NULL DEFAULT ARRAY[]::TEXT[],
    companies_synced_at  TIMESTAMPTZ,
    contacts_synced_at   TIMESTAMPTZ,
    last_attempted_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_error           TEXT,
    attempts             INT         NOT NULL DEFAULT 0,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (note_id, project_id)
);

CREATE INDEX IF NOT EXISTS hubspot_project_note_sync_note_idx
    ON hubspot_project_note_sync (note_id);

CREATE INDEX IF NOT EXISTS hubspot_project_note_sync_project_idx
    ON hubspot_project_note_sync (project_id);

-- Surfaces pairs that still owe work for retry sweeps and debugging.
CREATE INDEX IF NOT EXISTS hubspot_project_note_sync_unsynced_idx
    ON hubspot_project_note_sync (last_attempted_at DESC)
    WHERE companies_synced_at IS NULL OR contacts_synced_at IS NULL;

COMMIT;
