# SubscriptionFlow — How It Actually Works

SubscriptionFlow ("SF") is the billing platform. This doc explains the whole SF
integration end to end: the three Lambdas, the database tables, and the non-obvious
things we learned building it. If you just want the authoritative one-liners
(function names, schedules, secrets), see `docs/system_reference.md`; this doc is the
narrative.

## The big picture

There are **three** SF Lambdas, doing three unrelated jobs:

```
                         (on demand, Function URL)
  caller ──► gymlaunch-sf-create-custom-weekly-sub-for-go-product ──► creates a sub in SF

                         (daily 06:30 UTC)
  SF API  ──► gymlaunch-subscriptionflow-daily-sync ──► RDS  (sf_* tables)

                         (daily 06:45 UTC, right after the sync)
  RDS     ──► gymlaunch-sync-sf-billing-info-to-hubspot ──► HubSpot company properties
```

1. **Subscribe endpoint** — creates a subscription in SF on demand (the original reason
   we integrated SF).
2. **Data sync** — mirrors SF's customers/subscriptions/invoices/transactions/products
   into RDS every morning.
3. **Billing push** — reads those RDS tables, computes 6 billing fields per company, and
   writes them onto the matching HubSpot **company** so the billing team sees them in HubSpot.

They share one thing: **auth** (below). Otherwise they're independent — you can touch one
without the others.

## Auth: OAuth2 client-credentials, token cached in the DB

SF uses an OAuth2 **client-credentials** grant. The long-lived `client_id` / `client_secret`
live in Secrets Manager (`gymlaunch/subscriptionflow/api`) and are injected as Lambda env
vars at deploy. The short-lived **bearer access token** is cached in the DB table
`subscriptionflow_oauth_token` (a single row — `id = 1`).

Any SF-calling Lambda:
- reads the cached token,
- refreshes it **proactively** if it's expired/near-expiry,
- and rotates **reactively** on a `401` (re-POST the credentials, save the new token, retry once).

That's why you'll see `[token] rotated` lines in the logs occasionally — normal, self-healing.
The token is *derived* state (re-mintable any time from the client credentials), which is why
it lives in the DB and **not** in Secrets Manager. Only the static credentials belong in SM.

## 1. The subscribe endpoint — `gymlaunch-sf-create-custom-weekly-sub-for-go-product`

**Source:** `src/subscriptionflow/create_sub/` · **Trigger:** Lambda Function URL (manually
managed — the deploy user can't create Function URLs, so it's enabled in the console).

Takes a JSON POST (`id` or `email`, optional `price`/dates/product ids) and:
1. finds the SF customer by `id`, else by email, else creates one;
2. creates a **1-year Termed** subscription (`type: "Termed"`, `termed_initial_period: 1 year`,
   `renewal_type: "Renew with Specific Term"`, `is_auto_renew: 0` → it ends after a year).

**Who calls it (the trigger chain):** an **"agreements" workflow** creates a PandaDoc agreement
(template id `eqaZLY8q6WWBoKiXrXtPcZ`). When the customer **signs** that PandaDoc, a **Zapier zap**
(editor: `https://zapier.com/editor/369877019/published`) fires the request at this Function URL.
So if you need to change what's sent to the subscribe endpoint (fields, price, product), **edit that
Zap** — not this Lambda.

Key behaviors:
- **`pay_invoice` is OFF** — the invoice is left DUE so SF doesn't auto-charge or fire
  payment-success logic.
- **`price` defaults to `0.00`** when omitted (intentional — corrected after the fact).
- The **weekly cadence comes from the SF plan config**, not the payload. Confirm the default
  plan is a weekly plan in the SF dashboard.
- **`DEBUG=1`** makes it a safe dry run: authenticates, looks up the customer read-only, and
  returns the subscription body it *would* post — creating nothing.

## 2. The data sync — `gymlaunch-subscriptionflow-daily-sync`

**Source:** `src/subscriptionflow/sync/` · **Schedule:** `cron(30 6 * * ? *)` (06:30 UTC).

Mirrors five SF objects into RDS: **customer, subscription, invoice, transaction, product**
(the `sf_*` tables). Processed in that order (smallest/most-valuable first; the ~116k-row
customer table last so it never starves the billing/churn data).

### How it pulls (the hard-won part)

- **Backfill** (first load): `GET /<obj>/with-relations`, **Laravel page-based** pagination
  (`?page` / `meta.last_page` / `links.next`). We do **not** detail-fetch each record — the
  `/with-relations` list gives flat fields + `customer_id` but omits the nested `items[]`/
  `invoices[]` arrays, and a detail call per record was an N+1 that timed out (116k customers).
- **Incremental** (every run after backfill): `POST /<obj>/filter` with
  `filter[updated_at][$gte] = last_watermark − 6h`, offset-paginated. **The plain list/
  with-relations endpoints IGNORE `filter[updated_at]`** (they re-return everything) — only
  `/filter`, the conditional-query endpoint, honors it. This is why incremental and backfill
  use different endpoints.
- **Upsert by SF `id`**, per page, committing as it goes.

### Resumability (why there's a state table)

116k customers at ~2s/API page is ~20 min — past Lambda's 15-min ceiling. So the sync:
- upserts **each page immediately** (durable progress),
- checkpoints the next page in **`sf_sync_state`**,
- and **stops ~60s before the deadline**, resuming from the checkpoint next invocation.

Once an object's full backfill completes, `sf_sync_state.backfill_done` flips to `true` and it
switches to incremental. Daily incremental runs finish in ~10 seconds.

### Monitoring / operating the sync

```sql
-- progress + freshness, one row per object
SELECT object_type, backfill_done, backfill_next_page, last_watermark
FROM sf_sync_state ORDER BY object_type;
```
- `backfill_done = false` → still doing the first full load; watch `backfill_next_page` climb.
- After backfill, watch `last_watermark` advance daily — if it stops, the sync isn't running.
- **`DEBUG=1`** = dry run (fetch one small page per object, write nothing, return a sample +
  pagination meta + a request trace). Safe.
- **`FULL_SYNC=1`** = force a re-backfill of already-completed objects (set for ONE run, then
  off). Not needed normally — empty tables backfill on their own.

### What's deferred (important for the billing push)

Because we dropped the per-record detail fetch, the **nested** fields never made it into the
tables: `primary_subscription_id`, `primary_invoice_id`, `plan_id`, `billing_frequency` are
**NULL**. Everything billing/churn needs is in the flat fields; the nested stuff would require
detail-fetching (deferred — "Path B"). The full SF payload is kept in each table's `raw jsonb`
so nothing is lost.

## 3. The billing push — `gymlaunch-sync-sf-billing-info-to-hubspot`

**Source:** `src/subscriptionflow/hubspot_push/` · **Schedule:** `cron(45 6 * * ? *)` (06:45 UTC,
right after the sync).

One SQL rollup (`BILLING_SQL` in the handler) computes 6 fields per **company**
(`sf_customer.hubspot_id`), then batch-updates HubSpot companies (100/call). It writes **only
companies whose values changed** — each company's 6 values are md5-hashed and compared against
`sf_hubspot_push_state`; unchanged companies are skipped. No HubSpot reads. First run writes all
~613 billing-active companies; every run after is tiny.

### The 6 company properties (HubSpot internal names)

| Property | Type | How it's computed |
|---|---|---|
| `billing_status` | dropdown | Active sub + Overdue/Partially-Paid → **Past Due**; Active → **Current**; Suspended → **Past Due**; Pending → **Pending**; else **Cancelled** (cancel wins) |
| `outstanding_balance` | number | `Σ closing_balance` on invoices with status `Overdue`/`Partially Paid` |
| `current_billing_amount` | number | `total_amount` of the latest **recurring core-product** invoice (excl. `Projected`/`Void`; excl. products *Additional Programs* + *Testing*) |
| `billing_frequency` | string | cadence keyword parsed from that invoice's name (Weekly/Monthly/Quarterly/Annual; PIF for one-off) |
| `last_payment_date` | date | latest `Paid` Payment transaction date |
| `next_payment_date` | date | soonest future `next_bill_date` across Active subs |

### Scope + identity

- **Billing-active companies only** (≥1 subscription or invoice). Dormant HubSpot-only contacts
  aren't touched.
- Keyed by `sf_customer.hubspot_id`. **Non-numeric hubspot_ids are skipped** — SF sometimes holds
  a junk id (a known upstream mismatch we can't fix here); numeric-but-wrong ids surface as
  per-record errors in the response.
- **`DEBUG=1`** = dry run: compute + return `companies_computed`, `changed`, and a sample; writes
  nothing to HubSpot and touches no state.

### Product / frequency are "Path A" (text-based)

We don't have real `product_id` on invoices (see "deferred" above), so:
- **Product category** = the product-name prefix of the invoice `description` (`"Additional
  Programs -> …"`), excluding *Additional Programs* + *Testing* (names looked up from
  `sf_product`). ⚠️ If those names don't resolve in `sf_product`, the exclusion silently does
  nothing — spot-check occasionally.
- **Frequency** = a keyword in the invoice/plan name.

This is intentionally loose (billing agreed). The robust version — real `product_id` + the plan's
`billing_period` — is "Path B," bundled with proper product categorization as future work.

## The database tables

| Table | Migration | What it is |
|---|---|---|
| `subscriptionflow_oauth_token` | 013 | Singleton OAuth2 bearer-token cache (rotated in-band) |
| `sf_customer` | 015 | SF customers; carries `hubspot_id` (the company link) + `accounting_resource_id` (Sage Intacct id) |
| `sf_subscription` | 015 | SF subscriptions (status, term, next_bill_date, hubspot_deal_id) |
| `sf_invoice` | 015 | SF invoices (total_amount, received_payment, closing_balance, status, description) |
| `sf_transaction` | 015 | SF transactions (status, amount, decline_reason, date) — the churn event log |
| `sf_product` | 015 | SF product catalog (id → name) |
| `sf_sync_state` | 016 | Per-object sync cursor: backfill_done, backfill_next_page, last_watermark |
| `sf_hubspot_push_state` | 017 | Per-company md5 hash of last-pushed billing fields (change detection) |

All `sf_*` tables have TEXT primary keys (SF's own ids), a `raw jsonb` column with the full SF
payload, no hard foreign keys (loose coupling — indexed instead), and `gls_writer` grants.

**Identity note:** for HubSpot-sourced customers, `data_source = 'HubSpot'` and the SF customer id
*equals* the `hubspot_id`. `hubspot_id` is populated on customers, invoices, and subscriptions;
subscriptions also carry `additional_data.hubspot_deal_id`.

## Gotchas we hit (so you don't re-hit them)

- **Lambda names must be `gymlaunch-…` with hyphens, never underscores.** The `gymlaunch-deploy`
  IAM policy scopes Lambda actions to `gymlaunch-*` (literal hyphen). An underscore name deploys
  with `AccessDenied` on `lambda:GetFunction` and CloudFormation rolls the whole stack back. (This
  bit the billing push once — it was `gymlaunch_sync_…`.)
- **`/with-relations` ≠ detail.** The list endpoints omit `items[]`/`invoices[]`; only
  `GET /<obj>/{id}` has them. Hence the deferred nested fields.
- **Plain list ignores `filter[updated_at]`** → incremental uses `POST /<obj>/filter`.
- **`billing_frequency` is blank for prepaid-annual termed subs** (e.g. paid a year up front).
  The cadence lives in `plan_price.billing_period` (not synced) and the flat term fields can't
  tell "billed weekly over a year" from "prepaid the year at once." True one-off products *are*
  caught as PIF. Fix = Path B.
- **The deploy user can't read CloudWatch logs.** Read `DEBUG` dry-run output from the invocation
  response, or watch the DB tables (`sf_sync_state`, row counts) in SQL.

## What this does NOT do (yet)

- No churn scoring — the payment history is in `sf_transaction` waiting for it.
- No failed-payment alerting Lambda — the SF-dashboard webhook → Zapier is the current stopgap.
- No real product/plan categorization — Path A text-parsing is the placeholder.

See `docs/future_work.md` for the tracked follow-ons.
