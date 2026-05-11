-- HubSpot integration schema
-- account_manager_hubspot_map: maps Asana AM display names to HubSpot owner IDs.
-- Populated manually the first time; update when team changes.
-- Depends on: 001_foundation.sql

BEGIN;

CREATE TABLE IF NOT EXISTS account_manager_hubspot_map (
    name        TEXT PRIMARY KEY,
    hubspot_id  TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMIT;
