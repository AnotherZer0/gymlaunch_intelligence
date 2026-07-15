-- 018: Alert state for the agency-board -> HubSpot sync.
--
-- The stage-2 sync (gymlaunch-sync-agency-board-to-hubspot) emails an alert
-- when it hits ACTIONABLE failures: a value HubSpot rejects (e.g. a new Asana
-- status missing from the asana_agency_status dropdown) or an account manager
-- with no row in account_manager_hubspot_map. Parked 404s (dead company ids)
-- are deliberately excluded — those are procedural fixes in Asana.
--
-- Singleton row holds a fingerprint of the current problem set so we only
-- email when the set CHANGES, not on every hourly retry.

CREATE TABLE IF NOT EXISTS hubspot_sync_alert_state (
    id               SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    last_fingerprint TEXT NOT NULL DEFAULT '',
    last_problems    JSONB,
    last_alerted_at  TIMESTAMPTZ,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO hubspot_sync_alert_state (id) VALUES (1)
ON CONFLICT (id) DO NOTHING;

-- Lambdas connect as gls_writer; this migration is usually applied as the
-- personal admin user, so grant explicitly (idempotent either way).
GRANT SELECT, INSERT, UPDATE ON hubspot_sync_alert_state TO gls_writer;
