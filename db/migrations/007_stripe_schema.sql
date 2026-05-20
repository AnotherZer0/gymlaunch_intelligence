-- Tracks which Stripe payout IDs have already been exported.
-- One row per processed payout per account. Prevents duplicate reports.
CREATE TABLE stripe_payout_export (
    payout_id          VARCHAR(64)  PRIMARY KEY,
    stripe_account_id  VARCHAR(32)  NOT NULL,
    processed_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
