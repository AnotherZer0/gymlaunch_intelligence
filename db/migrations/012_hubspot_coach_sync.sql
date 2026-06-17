-- 012: HubSpot -> Asana coach sync column
--
-- Supports the daily Lambda gymlaunch-sync-hubspot-to-agency-board, which moves
-- the coach assignment one direction only: HubSpot company.coach -> DB -> Asana.
-- This is the "future plan" noted on the coach column back in migration 003.
--
-- hs_coach
--   Coach NAME (e.g. "Ryan") resolved from HubSpot's `coach` property, which is a
--   HubSpot *user id*. Written by Stage 1 (HubSpot -> DB). Left untouched when
--   HubSpot's coach is empty or the user id isn't in the roster map.
--
-- The sync pushes hs_coach onto the Asana Coach field only when it differs from
-- the live `coach` value (which the hourly Asana->DB sync mirrors here). So:
--   * a quiet day = zero Asana writes
--   * HubSpot always wins: a manual Asana coach edit is corrected on the next run
--
-- NOTE: the hourly Asana->DB sync never touches hs_coach (it's not in that sync's
-- ON CONFLICT ... DO UPDATE SET list), and the `coach` column keeps mirroring the
-- live Asana value as before. The "Agency Pro clears HubSpot coach" rule still
-- lives entirely in the outbound hourly sync and is unaffected.

ALTER TABLE asana_agency_board_task
    ADD COLUMN IF NOT EXISTS hs_coach TEXT;

-- Partial index for the Stage 2 "needs pushing to Asana" scan.
CREATE INDEX IF NOT EXISTS asana_agency_board_coach_push_idx
    ON asana_agency_board_task (task_gid)
    WHERE hubspot_company_id IS NOT NULL
      AND hs_coach IS NOT NULL;
