-- AI Brain Phase 1: client pulse summaries + activating the identity layer.
--
-- Two Lambdas land on top of this migration:
--   * gymlaunch-client-identity-resolver  — the first thing to ever WRITE to the
--     long-dormant client_account / client_external_id tables (001_foundation).
--     It derives each client's external IDs (Asana tasks, HubSpot companies,
--     Slack channel, owner emails) and upserts them.
--   * gymlaunch-client-pulse-summary      — reads a client's last 14 days across
--     Slack/Asana/Fathom (resolved via client_external_id) and writes an
--     AI-generated "pulse check" here.
--
-- client_period_summary
--   One row per (client, window). UNIQUE on the window so a re-run upserts the
--   same period instead of stacking duplicates. body is the AI summary text;
--   source_counts records how many slack/asana/fathom items fed the summary
--   (so an empty/garbage run is obvious); model records which model produced it.
--
-- client_account.active
--   Soft-deactivate for churned clients. The resolver flips this to false when a
--   client no longer has a live Asana card, instead of deleting their identity
--   rows and historical summaries. The pulse Lambda only runs on active clients.
--
-- Grants: client_account / client_external_id PREDATE the personal DB user (they
-- were created as gls_writer in 001), so they technically need nothing — but the
-- resolver is the first writer, so we assert the grants explicitly and
-- idempotently here per the project's migration-grant rule. The brand-new
-- client_period_summary table + sequence DO need grants: a table created by an
-- admin/personal user grants gls_writer nothing, and the Lambda would hit
-- "42501 permission denied" at runtime without them.
--
-- Idempotent (IF NOT EXISTS / ADD COLUMN IF NOT EXISTS / idempotent grants).
--
-- Depends on: 001_foundation.sql (client_account, client_external_id)

BEGIN;

-- Soft-deactivate flag for churned clients (keep identity + history).
ALTER TABLE client_account
    ADD COLUMN IF NOT EXISTS active BOOLEAN NOT NULL DEFAULT true;

CREATE TABLE IF NOT EXISTS client_period_summary (
    id                 BIGSERIAL   PRIMARY KEY,
    client_account_id  BIGINT      NOT NULL REFERENCES client_account (id) ON DELETE CASCADE,
    period_start       DATE        NOT NULL,
    period_end         DATE        NOT NULL,
    body               TEXT,                    -- AI-generated pulse summary
    source_counts      JSONB,                   -- e.g. {"slack": 42, "asana": 7, "fathom": 2}
    model              TEXT,                    -- model id that produced `body`
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT client_period_summary_window_uniq
        UNIQUE (client_account_id, period_start, period_end)
);

CREATE INDEX IF NOT EXISTS client_period_summary_client_idx
    ON client_period_summary (client_account_id, period_end DESC);

COMMENT ON TABLE  client_period_summary             IS 'AI "pulse check" summaries of a client''s experience over a time window, built from Slack/Asana/Fathom.';
COMMENT ON COLUMN client_period_summary.source_counts IS 'Per-source item counts that fed the summary; a zeroed source flags a broken/empty run.';

-- New table -> grant the verbs the pulse Lambda uses (SELECT + upsert).
GRANT SELECT, INSERT, UPDATE ON client_period_summary TO gls_writer;
GRANT USAGE, SELECT ON SEQUENCE client_period_summary_id_seq TO gls_writer;

-- Assert grants on the identity tables the resolver now writes (idempotent).
GRANT SELECT, INSERT, UPDATE ON client_account TO gls_writer;
GRANT SELECT, INSERT, UPDATE, DELETE ON client_external_id TO gls_writer;
GRANT USAGE, SELECT ON SEQUENCE client_account_id_seq TO gls_writer;
GRANT USAGE, SELECT ON SEQUENCE client_external_id_id_seq TO gls_writer;

COMMIT;
