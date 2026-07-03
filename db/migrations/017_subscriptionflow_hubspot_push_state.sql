-- State for the SF billing-field push to HubSpot
-- (gymlaunch_sync_sf_billing_info_to_hubspot).
--
-- The push recomputes every billing-active company's 6 billing fields from the sf_*
-- tables each run, but to keep HubSpot API calls (and property-history noise) minimal it
-- only writes companies whose values actually CHANGED. This table stores a hash of the
-- last-pushed field set per HubSpot company id; a company is pushed only when its freshly
-- computed hash differs (or is new). No HubSpot reads needed — we compare against our own
-- hash. Doubles as a resume point: if a run stops early, un-pushed companies still have a
-- stale/absent hash and get picked up next run.
--
-- Depends on: 015_subscriptionflow_data_schema.sql

BEGIN;

CREATE TABLE IF NOT EXISTS sf_hubspot_push_state (
    hubspot_id  text        PRIMARY KEY,   -- HubSpot company id
    fields_hash text        NOT NULL,      -- md5 of the last-pushed billing property set
    pushed_at   timestamptz NOT NULL DEFAULT now()
);

GRANT SELECT, INSERT, UPDATE ON sf_hubspot_push_state TO gls_writer;

COMMIT;
