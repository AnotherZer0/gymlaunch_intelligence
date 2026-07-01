-- SubscriptionFlow sync cursor/state.
--
-- The daily sync (gymlaunch-subscriptionflow-daily-sync) backfills large tables
-- (customers alone are ~116k rows / ~580 API pages) that can't complete inside
-- Lambda's 15-minute ceiling in one shot. This table lets a run checkpoint its
-- progress and resume on the next invocation, then switch to incremental once the
-- backfill is done.
--
--   backfill_done       false until the full paginated backfill has completed once.
--   backfill_next_page  the next page to fetch while backfilling (resume point).
--   last_watermark      max(updated_at) synced so far; incremental runs pull
--                       records changed at/after this (minus a lookback buffer).
--
-- One row per SF object type ('customer','subscription','invoice','transaction','product').
--
-- Depends on: 014_subscriptionflow_data_schema.sql

BEGIN;

CREATE TABLE IF NOT EXISTS sf_sync_state (
    object_type         text PRIMARY KEY,
    backfill_done       boolean     NOT NULL DEFAULT false,
    backfill_next_page  integer     NOT NULL DEFAULT 1,
    last_watermark      timestamptz,
    updated_at          timestamptz NOT NULL DEFAULT now()
);

GRANT SELECT, INSERT, UPDATE ON sf_sync_state TO gls_writer;

COMMIT;
