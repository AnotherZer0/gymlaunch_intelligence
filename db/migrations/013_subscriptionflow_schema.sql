-- SubscriptionFlow OAuth2 token store.
--
-- SubscriptionFlow (referred to as "SF") authenticates with an OAuth2
-- client-credentials grant (Laravel Passport). The client_id / client_secret
-- live in Lambda env vars; this table holds the short-lived bearer
-- access_token that those credentials are exchanged for.
--
-- Design notes
--   * SINGLE TENANT: there is exactly one SF account for GymLaunch, so this is
--     a singleton table — one row, enforced by the id = 1 CHECK. If we ever go
--     multi-tenant (per-client SF accounts), drop the singleton constraint and
--     add an account key column. Tracked in docs/future_work.md.
--   * The access_token is rotated in-band by the subscribe Lambda
--     (gymlaunch-sf-create-custom-weekly-sub-for-go-product): on a 401 from SF,
--     or when expires_at has passed, it re-POSTs the client credentials to the
--     token endpoint and UPSERTs the new token here, then retries the call.
--   * client_credentials grants do NOT return a refresh_token — a "refresh" is
--     just re-requesting with the stored credentials — so there is no
--     refresh_token column by design.
--
-- Idempotent via IF NOT EXISTS so it's safe to re-run.
--
-- Depends on: 001_foundation.sql

BEGIN;

CREATE TABLE IF NOT EXISTS subscriptionflow_oauth_token (
    id           smallint    PRIMARY KEY DEFAULT 1,
    access_token text        NOT NULL,
    token_type   text        NOT NULL DEFAULT 'Bearer',
    expires_at   timestamptz,             -- best-effort: now() + expires_in at fetch time
    obtained_at  timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT subscriptionflow_oauth_token_singleton CHECK (id = 1)
);

COMMENT ON TABLE  subscriptionflow_oauth_token              IS 'Singleton store for the SubscriptionFlow OAuth2 client-credentials bearer token.';
COMMENT ON COLUMN subscriptionflow_oauth_token.expires_at   IS 'Approximate expiry (now() + expires_in when the token was fetched); used to refresh proactively before a 401.';
COMMENT ON COLUMN subscriptionflow_oauth_token.obtained_at  IS 'When this token was first fetched.';

-- The Lambda connects as gls_writer, but this migration may be applied by a
-- different role (e.g. an admin/personal user). Unlike a table gls_writer owns,
-- one created by another role grants it nothing — so grant explicitly. The
-- handler does SELECT + INSERT ... ON CONFLICT DO UPDATE, hence these three.
-- Idempotent. (No sequence to grant: `id` is a plain smallint, not a serial.)
GRANT SELECT, INSERT, UPDATE ON subscriptionflow_oauth_token TO gls_writer;

COMMIT;
