-- Add hl_sub_account_api_key column to client_lead_master.
--
-- Originally excluded from migration 010 to avoid propagating plaintext GHL
-- keys into RDS. Decision reversed: the keys are already broadly visible (the
-- source sheet is shared internally; every GHL admin at every client can read
-- their own key), and we'll need them in RDS for future per-client automation
-- against GHL (FB lead forwarder replacement, contact pulls, etc.). Treating
-- them as service identifiers rather than tightly-held secrets.
--
-- A future hardening pass — tracked in `docs/future_work.md` under "Harden GHL
-- API key storage" — may move these to Secrets Manager (Option B2 in that
-- entry). When that lands, this column gets dropped.
--
-- Idempotent via IF NOT EXISTS so it's safe to re-run regardless of state.
--
-- Depends on: 010_client_lead_master_schema.sql

BEGIN;

ALTER TABLE client_lead_master
    ADD COLUMN IF NOT EXISTS hl_sub_account_api_key TEXT;

COMMIT;
