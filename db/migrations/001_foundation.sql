-- Foundation: tenancy + client identity (see docs/agend_db_and_strategy_49bb15d9.plan.md)
-- Run after connecting to your app database (not necessarily the default "postgres" DB).

BEGIN;

CREATE TABLE IF NOT EXISTS organization (
    id          BIGSERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS client_account (
    id               BIGSERIAL PRIMARY KEY,
    organization_id  BIGINT REFERENCES organization (id) ON DELETE RESTRICT,
    name             TEXT NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS client_account_org_idx
    ON client_account (organization_id);

CREATE TABLE IF NOT EXISTS client_external_id (
    id                 BIGSERIAL PRIMARY KEY,
    client_account_id  BIGINT NOT NULL REFERENCES client_account (id) ON DELETE CASCADE,
    system             TEXT NOT NULL,
    id_type            TEXT NOT NULL,
    value              TEXT NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT client_external_id_system_id_type_value_uniq
        UNIQUE (system, id_type, value)
);

CREATE INDEX IF NOT EXISTS client_external_id_client_idx
    ON client_external_id (client_account_id);

CREATE INDEX IF NOT EXISTS client_external_id_system_idx
    ON client_external_id (system);

COMMIT;
