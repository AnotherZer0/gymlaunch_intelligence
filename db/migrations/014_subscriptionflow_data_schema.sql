-- SubscriptionFlow ("SF") data-lake tables: customer, subscription, invoice,
-- transaction, product.
--
-- Populated by a daily sync of SF payment data into RDS. Serves three consumers:
--   1. Billing-field push to HubSpot (invoice amount due/paid, outstanding, status).
--   2. Churn signal for the AI brain (payment behaviour over time — failed
--      transactions, cancellations, slipping payments).
--   3. The failed-payment alerting reconciliation backbone.
--
-- Design (confirmed against live API responses, 2026-06-30):
--   * PKs are SF's own ids (TEXT): UUIDs for subscription/invoice/transaction/product,
--     numeric-strings for customer.
--   * `hubspot_id` is a REAL, populated SF field on customer/invoice/subscription — the
--     deterministic link to HubSpot. For HubSpot-sourced customers (data_source =
--     'HubSpot') the SF customer id EQUALS hubspot_id. Nullable for SF-created customers
--     (e.g. those our subscribe Lambda creates) until they sync back. Subscriptions also
--     carry additional_data.hubspot_deal_id -> hubspot_deal_id.
--   * Cross-object links are embedded in SF detail responses, so we denormalise the
--     common single-value link (primary_subscription_id / primary_invoice_id) AND keep
--     the entire payload in `raw jsonb` so nothing is lost and multi-line invoices /
--     multi-invoice transactions stay representable.
--   * billing_frequency is derived at sync time from the embedded
--     plan_price.billing_period_months_weeks (e.g. 13 weeks = quarterly) — NOT from the
--     term fields (a live sub showed termed_initial_period 52/Week while billing quarterly).
--   * accounting_resource_id = Sage Intacct id. data_source = origin
--     (HubSpot / SubscriptionFlow / SubscriptionFlow(HPP)).
--   * NO hard foreign keys: sync order across objects isn't guaranteed, so links are
--     indexed columns, not constraints (loose coupling, standard for a sync mirror).
--   * synced_at = when OUR sync last wrote the row (vs SF's updated_at).
--
-- Grants: the Lambda connects as gls_writer, but this migration may be applied by the
-- personal/admin DB user, so grant explicitly (CLAUDE.md rule). The sync does
-- SELECT + INSERT ... ON CONFLICT DO UPDATE, hence SELECT/INSERT/UPDATE. No sequences
-- to grant (all PKs are TEXT, not serial).
--
-- Depends on: 001_foundation.sql

BEGIN;

-- ---------- Customers ----------
CREATE TABLE IF NOT EXISTS sf_customer (
    id                          text PRIMARY KEY,            -- SF customer id (== hubspot_id when HubSpot-sourced)
    hubspot_id                  text,                        -- deterministic HubSpot link (nullable for SF-created)
    accounting_resource_id      text,                        -- Sage Intacct customer id (e.g. C-5282)
    name                        text,
    email                       text,
    phone_number                text,
    currency                    text,
    auto_charge                 boolean,                     -- SF sends 0/1; sync coerces
    data_source                 text,                        -- HubSpot / SubscriptionFlow / ...
    primary_churn_score_value   numeric,                     -- SF native churn score (often null)
    primary_churn_score_grade   text,
    parent_id                   text,                        -- parent customer (agency -> sub-accounts)
    created_at                  timestamptz,
    updated_at                  timestamptz,                 -- SF last-modified (incremental sync key)
    raw                         jsonb NOT NULL,              -- full API attributes (future-proof)
    synced_at                   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS sf_customer_hubspot_id_idx ON sf_customer (hubspot_id);
CREATE INDEX IF NOT EXISTS sf_customer_email_idx      ON sf_customer (lower(email));
CREATE INDEX IF NOT EXISTS sf_customer_updated_at_idx ON sf_customer (updated_at);

-- ---------- Subscriptions ----------
CREATE TABLE IF NOT EXISTS sf_subscription (
    id                          text PRIMARY KEY,
    name                        text,                        -- SF-001619
    display_name                text,
    hubspot_id                  text,
    hubspot_deal_id             text,                        -- additional_data.hubspot_deal_id
    customer_id                 text,
    status                      text,                        -- Active / Cancelled / Suspended / ...
    payment_status              text,                        -- UI "Invoice Status" rollup (Paid / ...)
    type                        text,                        -- Termed / Evergreen
    termed_start_date           date,
    termed_initial_period       integer,
    termed_initial_period_type  text,
    renewal_type                text,
    renewal_period              integer,
    renewal_period_type         text,
    is_auto_renew               boolean,
    billing_end_date            date,
    next_bill_date              date,                        -- billing's "Next Payment Date"
    billing_frequency           text,                        -- derived from plan_price.billing_period_months_weeks
    total_amount                numeric,                     -- full term value
    plan_id                     text,
    product_id                  text,
    plan_price_id               text,
    suspended_at                timestamptz,
    cancelled_at                timestamptz,                 -- churn signal
    renewed_at                  timestamptz,
    mv_remaining_term           text,                        -- e.g. "362 Days"
    data_source                 text,
    created_at                  timestamptz,
    updated_at                  timestamptz,
    raw                         jsonb NOT NULL,              -- full payload incl. items[]/charges[]/plan_price
    synced_at                   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS sf_subscription_customer_id_idx ON sf_subscription (customer_id);
CREATE INDEX IF NOT EXISTS sf_subscription_status_idx      ON sf_subscription (status);
CREATE INDEX IF NOT EXISTS sf_subscription_hubspot_id_idx  ON sf_subscription (hubspot_id);
CREATE INDEX IF NOT EXISTS sf_subscription_updated_at_idx  ON sf_subscription (updated_at);

-- ---------- Invoices ----------
CREATE TABLE IF NOT EXISTS sf_invoice (
    id                          text PRIMARY KEY,
    name                        text,                        -- SF-15579
    hubspot_id                  text,
    customer_id                 text,
    primary_subscription_id     text,                        -- items[0].subscription_id (most invoices are single-item)
    invoice_date                date,
    due_date                    date,
    status                      text,                        -- Paid / Due / Draft / ...
    sub_total                   numeric,
    total_amount                numeric,                     -- billing "Current Billing Amount" / amount due
    received_payment            numeric,                     -- billing amount paid
    opening_balance             numeric,
    closing_balance             numeric,                     -- billing outstanding (per invoice)
    sum_of_credit_notes         numeric,
    tax_amount                  numeric,
    discount_value              numeric,
    currency                    text,
    is_oneoff                   boolean,
    description                 text,
    note                        text,
    data_source                 text,
    created_at                  timestamptz,
    updated_at                  timestamptz,
    raw                         jsonb NOT NULL,              -- full payload incl. items[] (+subscription_id) & transactions[]
    synced_at                   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS sf_invoice_customer_id_idx  ON sf_invoice (customer_id);
CREATE INDEX IF NOT EXISTS sf_invoice_subscription_idx ON sf_invoice (primary_subscription_id);
CREATE INDEX IF NOT EXISTS sf_invoice_status_idx       ON sf_invoice (status);
CREATE INDEX IF NOT EXISTS sf_invoice_updated_at_idx   ON sf_invoice (updated_at);

-- ---------- Transactions ----------
CREATE TABLE IF NOT EXISTS sf_transaction (
    id                          text PRIMARY KEY,
    name                        text,                        -- P-013336
    number                      text,
    gateway_transaction_id      text,                        -- SF "transaction_id" (payment processor ref)
    customer_id                 text,
    primary_invoice_id          text,                        -- invoices[0].invoice_id
    date                        timestamptz,
    status                      text,                        -- success seen as "Paid"; failures TBD (Failed/Declined)
    amount                      numeric,
    balance                     numeric,
    unapplied_amount            numeric,
    type                        text,                        -- Transaction / Refund / ...
    transaction_category        text,                        -- Payment / ... (filter to Payment for churn)
    cash_or_card                text,
    payment_type_id             text,                        -- card mask e.g. "****7986"
    payment_method_id           text,
    decline_reason              text,                        -- churn gold (null on success)
    reason_code                 text,
    reference_transaction_id    text,                        -- retry / refund chain
    description                 text,
    currency                    text,
    data_source                 text,
    created_at                  timestamptz,
    updated_at                  timestamptz,
    raw                         jsonb NOT NULL,              -- full payload incl. invoices[] links
    synced_at                   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS sf_transaction_customer_id_idx ON sf_transaction (customer_id);
CREATE INDEX IF NOT EXISTS sf_transaction_invoice_idx     ON sf_transaction (primary_invoice_id);
CREATE INDEX IF NOT EXISTS sf_transaction_status_idx      ON sf_transaction (status);
CREATE INDEX IF NOT EXISTS sf_transaction_date_idx        ON sf_transaction (date);

-- ---------- Products (reference / catalog) ----------
CREATE TABLE IF NOT EXISTS sf_product (
    id                          text PRIMARY KEY,
    name                        text,
    description                 text,
    sku                         text,
    position                    integer,
    image                       text,
    data_source                 text,
    created_at                  timestamptz,
    updated_at                  timestamptz,
    raw                         jsonb NOT NULL,
    synced_at                   timestamptz NOT NULL DEFAULT now()
);

-- gls_writer is the Lambda's DB role; grant explicitly (see CLAUDE.md "Database migrations").
GRANT SELECT, INSERT, UPDATE ON
    sf_customer, sf_subscription, sf_invoice, sf_transaction, sf_product
TO gls_writer;

COMMIT;
