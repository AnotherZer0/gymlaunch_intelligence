# GymLaunch Intelligence — System Reference

Complete reference for all infrastructure, Lambda functions, secrets, database objects,
and source files. Use this when deploying, debugging, or onboarding.

---

## Deployment

**Tool:** AWS SAM  
**Config:** `samconfig.toml` (project root)  
**Template:** `infra/template.yaml`  
**Deploy script:** `scripts/deploy.sh`

To deploy all Lambdas:
```bash
bash scripts/deploy.sh
```

The script pulls all secrets from Secrets Manager, runs `sam build`, then `sam deploy`.
It also sets 30-day log retention on all five Lambda log groups.

**CloudFormation stack name:** `gymlaunch-slack-sync`
(Note: the stack is named after the first Lambda ever deployed into it — the name is misleading
but changing it would require tearing down and recreating all resources, so it stays.)

---

## Lambda Functions

| Function name | Schedule | Source |
|---|---|---|
| `gymlaunch-slack-sync` | Every hour | `src/sync/slack/` |
| `gymlaunch-asana-agency-board-sync` | Every hour, top of hour | `src/sync/asana/agency_board/` |
| `gymlaunch-asana-agency-board-deep-sync` | Once daily | `src/sync/asana/deep/` |
| `gymlaunch-sync-agency-board-to-hubspot` | Every hour, :10 past | `src/sync/hubspot/` |
| `gymlaunch-sync-hubspot-to-agency-board` | Once daily | `src/sync/hubspot_to_asana/` |
| `gymlaunch-mb-capacity-sheet-sync` | 8am, 12pm, 4pm, 8pm EDT | `src/sync/sheets/` |
| `gymlaunch-sms-interceptor` | HTTP webhook (API Gateway) | `src/sms/interceptor/` |
| `gymlaunch-phone-validator` | HTTP webhook (API Gateway) | `src/phone/validator/` |
| `gymlaunch-stripe-finance-report` | M-F at 10am Central (15:00 UTC) | `src/sync/stripe/finance_report/` |
| `gymlaunch-project-note-sync` | Every 72 hours | `src/sync/hubspot_project_notes/` |
| `gymlaunch-fathom-webhook` | HTTP webhook (API Gateway) | `src/fathom/webhook/` |
| `gymlaunch-fathom-daily-sync` | Daily at 04:00 UTC (11pm CDT) | `src/fathom/sync/` |
| `gymlaunch-supabase-lead-sync` | Daily at 08:00 UTC (2am CST / 3am CDT) | `src/sync/supabase_leads/` |
| `gymlaunch-lead_db2-sheet-sync` | Daily at 07:00 UTC (one hour before supabase-lead-sync) | `src/sync/lead_db2_sheet/` |
| `gymlaunch-add-slack-channel` | Function URL (on-demand, HubSpot-triggered) — **manually managed, see note** | `src/slack/add_channel/` |
| `gymlaunch-sf-create-custom-weekly-sub-for-go-product` | Function URL (on-demand) — **manually managed, see note** | `src/subscriptionflow/create_sub/` |
| `gymlaunch-subscriptionflow-daily-sync` | Daily at 06:30 UTC | `src/subscriptionflow/sync/` |
| `gymlaunch-sync-sf-billing-info-to-hubspot` | Daily at 06:45 UTC (right after the sync) | `src/subscriptionflow/hubspot_push/` |
| `gymlaunch-client-identity-resolver` | Once daily | `src/identity/resolver/` |
| `gymlaunch-client-pulse-summary` | Every 14 days | `src/pulse/summary/` |

All functions run in the VPC (subnets `subnet-a085c381`, `subnet-3d566b33`) so they can reach RDS.  
All share IAM role `gymlaunch-slack-sync` (role named after first Lambda — same naming quirk as stack).  
Permissions boundary: `gymlaunch-lambda-boundary`.

**Note — `gymlaunch-add-slack-channel` and `gymlaunch-sf-create-custom-weekly-sub-for-go-product` are NOT in the SAM stack.** The deploy IAM user
cannot create Lambda Function URLs (`lambda:CreateFunctionUrlConfig` isn't granted, and
`lambda:AddPermission` is limited to the `events`/`apigateway` principals). So this function
is managed manually: code in `src/slack/add_channel/` pushed via `aws lambda update-function-code`,
and its Function URL + public permission added in the console. `deploy.sh` only sets its log retention.

### CloudWatch Log Groups

```
/aws/lambda/gymlaunch-slack-sync
/aws/lambda/gymlaunch-asana-agency-board-sync
/aws/lambda/gymlaunch-asana-agency-board-deep-sync
/aws/lambda/gymlaunch-sync-agency-board-to-hubspot
/aws/lambda/gymlaunch-sync-hubspot-to-agency-board
/aws/lambda/gymlaunch-mb-capacity-sheet-sync
/aws/lambda/gymlaunch-sms-interceptor
/aws/lambda/gymlaunch-phone-validator
/aws/lambda/gymlaunch-stripe-finance-report
/aws/lambda/gymlaunch-project-note-sync
/aws/lambda/gymlaunch-fathom-webhook
/aws/lambda/gymlaunch-fathom-daily-sync
/aws/lambda/gymlaunch-supabase-lead-sync
/aws/lambda/gymlaunch-lead_db2-sheet-sync
/aws/lambda/gymlaunch-add-slack-channel
/aws/lambda/gymlaunch-sf-create-custom-weekly-sub-for-go-product
/aws/lambda/gymlaunch-client-identity-resolver
/aws/lambda/gymlaunch-client-pulse-summary
```

Retention: 30 days (set by deploy script).

---

## Secrets Manager

All secrets are in `us-east-1`.

| Secret path | Key inside JSON | Used by |
|---|---|---|
| `gymlaunch/db/gls_writer` | `gls_writer` | All Lambdas (DB password) |
| `gymlaunch/slack/bot_token` | `bot_token` | `gymlaunch-slack-sync` |
| `gymlaunch/asana/token` | `token` | `gymlaunch-asana-sync`, `gymlaunch-asana-deep-sync` |
| `gymlaunch/hubspot/token` | `token` | `gymlaunch-hubspot-sync` |
| `gymlaunch/google/service_account` | _(raw JSON)_ | `gymlaunch-mb-capacity-sheet-sync` |
| `gymlaunch/twilio/auth_token` | `auth_token` | `gymlaunch-sms-interceptor` |
| `gymlaunch/twilio/octopods_webhook_url` | `url` | `gymlaunch-sms-interceptor` |
| `gymlaunch/fathom/Fathom-Webhook-Secret` | `secret` | `gymlaunch-fathom-webhook` |
| `gymlaunch/stripe/api_keys` | _(raw JSON)_ | `gymlaunch-stripe-finance-report` |
| `gymlaunch/supabase/api` | `url` + `service_role_key` (raw JSON) | `gymlaunch-supabase-lead-sync` |
| `gymlaunch/slack/channel_add_key` | `api_key` | `gymlaunch-add-slack-channel` (Function URL secret) |
| `gymlaunch/fathom/Fathom-API-Key` | `api_key` | `gymlaunch-fathom-sync` (nightly sync, in progress) |
| `gymlaunch/subscriptionflow/api` | `client_id`, `client_secret`, `endpoint_api_key` | `gymlaunch-sf-create-custom-weekly-sub-for-go-product` |
| `gymlaunch/anthropic/api_key` | `api_key` | `gymlaunch-client-pulse-summary` |

The Google service account JSON is base64-encoded by deploy.sh and passed as
`GOOGLE_SERVICE_ACCOUNT_B64`. It must have Editor access to the Google Sheet.

---

## Database

**Host:** `gls.cdrq9b1h5qzb.us-east-1.rds.amazonaws.com`  
**Database:** `gymlaunch_intelligence`  
**Writer user:** `gls_writer`  
**Instance:** `db.t4g.micro`  
**Security group:** `sg-64359563`

### Migrations (run in order)

| File | What it creates |
|---|---|
| `db/migrations/001_foundation.sql` | Core schema foundations |
| `db/migrations/002_slack_schema.sql` | Slack tables |
| `db/migrations/003_asana_schema.sql` | Asana tables (`asana_task`, `asana_user`, `asana_agency_board_task`) |
| `db/migrations/004_staff_capacity.sql` | `agency_staff_capacity` table, seeds media buyers, creates `staff_availability` view |
| `db/migrations/005_hubspot_schema.sql` | `account_manager_hubspot_map` table |
| `db/migrations/006_sms_schema.sql` | `sms_inbound_message`, `sms_delivery_event` tables |
| `db/migrations/007_stripe_schema.sql` | `stripe_payout_export` table |
| `db/migrations/007_fathom_schema.sql` | `fathom_call`, `fathom_call_invitee` tables |
| `db/migrations/008_project_note_sync_schema.sql` | `hubspot_project_note_sync` table |
| `db/migrations/009_supabase_lead_sync_schema.sql` | 7 `supabase_*` mirror tables + `supabase_sync_state` |
| `db/migrations/010_client_lead_master_schema.sql` | `client_lead_master` — mirror of `00 - Database` sheet |
| `db/migrations/011_client_lead_master_add_api_key.sql` | Adds `hl_sub_account_api_key` column to `client_lead_master` |
| `db/migrations/012_hubspot_coach_sync.sql` | Adds `hs_coach` column to `asana_agency_board_task` for HubSpot→Asana coach sync |
| `db/migrations/013_subscriptionflow_schema.sql` | `subscriptionflow_oauth_token` table (singleton OAuth2 token cache) |
| `db/migrations/014_client_period_summary.sql` | `client_period_summary` table + `client_account.active` flag; asserts `gls_writer` grants on the identity tables (`client_account`, `client_external_id`) now that the resolver writes them |
| `db/migrations/015_subscriptionflow_data_schema.sql` | SF data lake: `sf_customer`, `sf_subscription`, `sf_invoice`, `sf_transaction`, `sf_product` (was numbered 014 — renumbered to avoid the collision with `014_client_period_summary`) |
| `db/migrations/016_subscriptionflow_sync_state.sql` | `sf_sync_state` — per-object sync cursor (resumable backfill + incremental watermark) |
| `db/migrations/017_subscriptionflow_hubspot_push_state.sql` | `sf_hubspot_push_state` — per-company md5 hash for the HubSpot billing-push change detection |
| `db/migrations/018_hubspot_sync_alert_state.sql` | `hubspot_sync_alert_state` — singleton fingerprint of the agency-board sync's actionable failures, drives change-only alert emails |

### Key Tables

| Table | Purpose |
|---|---|
| `asana_task` | All Asana tasks (raw) |
| `asana_user` | Asana users |
| `asana_agency_board_task` | Denormalized agency board rows with custom fields |
| `agency_staff_capacity` | Media buyer names + capacity limits |
| `account_manager_hubspot_map` | AM name → HubSpot company owner ID |
| `sms_inbound_message` | One row per inbound Twilio SMS — body, opt-out detection, HubSpot update outcome |
| `sms_delivery_event` | One row per Twilio StatusCallback — delivery failures, HubSpot suppression outcome |
| `stripe_payout_export` | One row per processed Stripe payout — prevents duplicate finance reports |
| `hubspot_project_note_sync` | One row per (note, project) pair processed by the nightly project-note sync. Snapshot of the project's companies/contacts + per-side synced_at timestamps. |
| `fathom_call` | One row per Fathom-recorded call — transcript, metadata, `summary` (AI-generated, nullable) |
| `fathom_call_invitee` | One row per invitee per call — indexed on `email` for AI brain queries |
| `supabase_facebook_lead` | Mirror of Supabase `02 - Facebook Leads`. One row per FB lead. Joins to `asana_agency_board_task` on `sub_account_id = hl_sub_account_location_id`. |
| `supabase_highlevel_lead` | Mirror of Supabase `03 - HighLevel Leads`. One row per GHL lead. `source` is `hlold` or `hlnew` (both count as GHL). |
| `supabase_lead_form` | Mirror of Supabase `01 - Lead Form Database`. Small form metadata reference. |
| `supabase_lead_form_detail` | Mirror of Supabase `lead_forms` — richer form metadata with JSONB columns (questions, thank-you page config). |
| `supabase_facebook_form` | Mirror of Supabase `Facebook Form Database`. Legacy. PK is `facebook_page_id` alone (one row per page in source). |
| `supabase_facebook_lead_form` | Mirror of Supabase `Facebook Lead Form Database`. Legacy, near-empty. |
| `supabase_facebook_lead_legacy` | Mirror of Supabase `Facebook Leads Database`. Predecessor of `supabase_facebook_lead` with different column names. |
| `supabase_sync_state` | Per-table sync watermark — last run timestamp, row count, success/error status. PK: `table_name`. Despite the name, used as the generic state table by both `gymlaunch-supabase-lead-sync` and `gymlaunch-lead_db2-sheet-sync`. |
| `subscriptionflow_oauth_token` | Singleton OAuth2 access token cache for SubscriptionFlow API. Rotated in-band (proactive on expiry + reactive on 401). |
| `client_account` | Canonical client (one row per owner; multiple locations collapse into one). Defined in 001, first populated by `gymlaunch-client-identity-resolver`. `active=false` = churned (soft-deactivated, history kept). |
| `client_external_id` | Address book: maps a `client_account` to each system's IDs — `(system, id_type, value)` e.g. `asana/task`, `hubspot/company`, `slack/channel`, `fathom/email`. `UNIQUE (system, id_type, value)`. |
| `client_period_summary` | AI "pulse check" per (client, window) — `body`, `source_counts` (slack/asana/fathom item counts), `model`. Written by `gymlaunch-client-pulse-summary`. |
| `client_lead_master` | Mirror of the `00 - Database` tab in the `Lead Integration Database V2.0` Google Sheet. Carries both auto-written fields (basic FB/GHL/Asana identifiers, populated by the n8n FB-lead workflow) and 10 manually-maintained diagnostic flags including `is_system_user` (Yes/No — sheet header is `system_user`, renamed because that's a PG 16+ reserved word), `fb_app_status`, `ghl_connection`. PK: `facebook_page_id`. Includes `hl_sub_account_api_key` (plaintext) as of migration 011 — future hardening tracked in `docs/future_work.md`. |

### Key Views

| View | Purpose |
|---|---|
| `staff_availability` | Live join of capacity vs active client count per media buyer. Columns: `name`, `role`, `capacity`, `active_count`, `free_slots`. |

The view is a live query — no refresh needed. Active count is derived from
`asana_agency_board_task` filtered to active sections with a real media buyer assigned.

Query reference: `db/how_to_query.txt`

### Supabase → RDS table map

Quick lookup of what mirrors what in the daily `gymlaunch-supabase-lead-sync`.
Full context for these tables is in the [Supabase Lead Sync](#supabase-lead-sync) section.

| Supabase source table | RDS destination table | Notes |
|---|---|---|
| `02 - Facebook Leads` | `supabase_facebook_lead` | Active. Compare side A. |
| `03 - HighLevel Leads` | `supabase_highlevel_lead` | Active. Compare side B. `source` is `hlold` or `hlnew`. |
| `01 - Lead Form Database` | `supabase_lead_form` | Active. Form metadata reference. |
| `lead_forms` | `supabase_lead_form_detail` | Active. Richer form metadata (JSONB questions, thank-you page). |
| `Facebook Form Database` | `supabase_facebook_form` | Legacy. |
| `Facebook Lead Form Database` | `supabase_facebook_lead_form` | Legacy, near-empty. |
| `Facebook Leads Database` | `supabase_facebook_lead_legacy` | Legacy. Predecessor of `02 - Facebook Leads`. |
| `00 - Lead Integration Main Database` | _(not mirrored)_ | Same per-client metadata is in `asana_agency_board_task`. Also held `hl_sub_account_api_key` plaintext, which we deliberately don't propagate. |

---

## Google Sheet

**Sheet ID:** `1xp4H9SUHHNgFu9PB_fchFpn8c1JwrF-7u74I3qLe5KY`  
**Tab written to:** `DB Sync`  
**Columns:** `name`, `capacity`, `active_count`, `free_slots`

The `gymlaunch-mb-capacity-sheet-sync` Lambda clears and rewrites this tab 4x daily.
To change what gets synced, edit the query in `src/sync/sheets/handler.py` and redeploy.

---

## Asana

**Agency board project GID:** `1206006426591402`

- `gymlaunch-asana-sync` — syncs top-level tasks and board rows hourly
- `gymlaunch-asana-deep-sync` — syncs subtasks and comments for all known tasks daily

Both use PostgreSQL advisory locks to prevent concurrent execution.
The hourly sync commits per-task so a timeout never loses all progress.

How to add new Asana custom fields: `src/sync/asana/agency_board/how_to_update.txt`

---

## HubSpot

- `gymlaunch-sync-agency-board-to-hubspot` — runs at :10 past every hour
- Reads rows from `asana_agency_board_task` where `content_hash != last_synced_hash`
- Pushes changes to HubSpot via Batch Companies API (`/crm/v3/objects/companies/batch/update`)
- Special case: when `coach = "Agency Pro"`, clears the field via a separate PATCH
- Account manager mapping via `account_manager_hubspot_map` table — **every AM name
  on the Asana board must have a row here** (exact spelling); unmapped AMs sync
  without the owner field and HubSpot silently keeps the stale owner
- `DEBUG` env var (default `"0"`): dry-run mode — no HubSpot/DB writes, returns the
  would-send payload in the response. While `1`, the hourly runs are no-ops.

### Failure handling (added July 2026 after a month-long silent jam)

HubSpot's batch update is **atomic on validation errors** — one bad record 400s the
whole batch. The June 2026 outage: a new Asana status ("Onboard - Stalled") wasn't an
option on the HubSpot `asana_agency_status` dropdown, so every batch failed hourly for
a month. The sync now:

- **Falls back to per-record PATCHes** when a batch fails, so one poison record can't
  block the rest.
- **Stores HubSpot's real error** in `last_hubspot_run_status` (e.g. the INVALID_OPTION
  message naming the bad value) — diagnosable from the DB, no CloudWatch needed.
- **Normalizes company ids**: a pasted record URL (`.../record/0-2/<id>`) is reduced to
  the numeric id before sending.
- **Parks 404s** (deleted/nonexistent company ids): records the error but sets
  `last_synced_hash` so the row stops retrying hourly; editing the task in Asana
  changes the hash and un-parks it automatically. 400 validation errors keep retrying
  because their fix is usually HubSpot-side (e.g. adding a dropdown option).
- **Handles duplicate company ids** (two Asana tasks → one company): sends are deduped,
  but every task row gets its status written back.
- New-value checklist: adding a status option in Asana requires adding the same option
  to the HubSpot `asana_agency_status` property (watch for trailing whitespace in the
  Asana option name — values are trimmed before sending).
- **Alerting**: actionable failures (a value HubSpot rejects, or an AM missing from
  `account_manager_hubspot_map`) trigger an SES email to `ALERT_TO_ADDRESS`
  (template param `HubspotSyncAlertTo`, default daniel.tingle@gymlaunchsecrets.com).
  Parked 404s are excluded — dead ids are procedural fixes in Asana. Fires only when
  the problem set *changes* (fingerprint in `hubspot_sync_alert_state`, migration 018),
  so an unfixed problem doesn't email hourly; a failed send retries next run.

How to add new HubSpot fields: `src/sync/hubspot/how_to_update.txt`

---

## IAM

| Policy file | Attached to |
|---|---|
| `infra/iam/gymlaunch-deploy-policy.json` | `gymlaunch-deploy` IAM user |
| `infra/iam/gymlaunch-lambda-boundary.json` | All Lambda execution roles (permissions boundary) |

The deploy user has `ExplicitDenyDangerous` which blocks `lambda:DeleteFunction`.
This means renaming a Lambda in the template leaves the old one orphaned — it must be
deleted manually from the Lambda console.

The deploy user also cannot create Lambda Function URLs (`lambda:CreateFunctionUrlConfig`
not granted). So any Lambda needing a Function URL must be manually managed: code
deployed via `aws lambda update-function-code`, URL + permission added in the console.
Currently applies to `gymlaunch-add-slack-channel` and
`gymlaunch-sf-create-custom-weekly-sub-for-go-product`.

---

## SMS Infrastructure

### API Gateway

**Stack resource:** `SmsApi` (HTTP API, `$default` stage)
**Base URL:** Retrieved from CloudFormation output `SmsApiUrl` after first deploy.

| Route | Purpose |
|---|---|
| `POST /sms/inbound` | Twilio inbound message webhook |
| `POST /sms/status` | Twilio StatusCallback (delivery events) |
| `POST /phone/validate` | Phone normalization + Twilio Lookup (called from HubSpot workflows) |
| `POST /fathom/webhook` | Fathom call transcript webhook (fired by Zapier) |

### gymlaunch-sms-interceptor

Validates the Twilio HMAC-SHA1 signature on every request, writes to RDS, runs opt-out /
delivery-failure logic, then forwards the raw body to Octopods.

**Last-message tracking** (`/sms/inbound`): EVERY inbound message triggers a HubSpot
contact lookup (mobilephone first, phone as fallback, multiple format variants in one
API call). If found, the contact's `last_twilio_sms_message_content` and
`last_twilio_sms_received` are updated. Unknown numbers are logged and skipped.
This replaced the HubSpot workflow that previously wrote the last-message field.

**Opt-out handling** (`/sms/inbound`): if the message body exactly matches a CTIA keyword
(STOP, STOPALL, UNSUBSCRIBE, CANCEL, END, QUIT, OPTOUT, REVOKE), the opt-out properties
are merged into the same PATCH as the last-message update — one API call per message.

**Delivery failure handling** (`/sms/status`): if `ErrorCode` is in `{30006, 30007, 30008}`,
the Lambda finds the recipient's HubSpot contact and sets `sms_deliverable = false` plus
`sms_ineligible_reason` to the mapped reason string.

**Failure fallback + alerting:** HubSpot rejects an entire PATCH if any single property
is invalid. On a failed PATCH the Lambda retries each property individually (good ones
land, the bad one is isolated), then sends an SES alert to `ALERT_TO_ADDRESS` naming the
failed properties. The outcome is recorded in `sms_inbound_message.hubspot_update_status`
(`ok` / `ok_after_fallback` / `partial: ...` / `contact_not_found`).

**DEBUG dry-run:** set env var `DEBUG=1` in the Lambda console. All logic runs (signature
check, lookup, staging) but nothing is written; the HTTP response is a JSON body showing
what WOULD happen. Signature failures don't 403 in debug so it can be exercised with curl.
Every deploy resets the flag to off.

**HubSpot properties required** (must be created manually in portal 43776308):

| Property | Type | Set by |
|---|---|---|
| `last_twilio_sms_message_content` | Multi-line text | Every inbound message |
| `last_twilio_sms_received` | Date picker | Every inbound message (midnight UTC) |
| `sms_subscriptions` | Multiple checkbox | Enrollment state per channel (options: Marketing, Product Updates, Support) |
| `sms_marketing_opted_out` | Checkbox | STOP/START on Marketing channel |
| `sms_product_updates_opted_out` | Checkbox | STOP/START on Product Updates channel |
| `sms_marketing_opt_out_date` | Date picker | Set on Marketing STOP; not cleared on START. **API name is `opt_out`, not `opted_out`** — the wrong name shipped originally and silently 400'd every Marketing STOP until 2026-07 |
| `sms_deliverable` | Checkbox | Hard delivery failure (30006/30007/30008) |
| `sms_ineligible_reason` | Single-line text | `geo_block` / `carrier_violation` / `carrier_error` |

The `sms_subscriptions` internal option names must match the channel names in `_CHANNEL_PROPS` in `handler.py` exactly (case-sensitive).
Date-picker properties only accept midnight-UTC epoch milliseconds — a raw `time.time()` value is rejected with `INVALID_DATE`.

### Twilio signature validation

Every request Twilio sends includes an `X-Twilio-Signature` header. We validate it on every inbound and status request — invalid signatures get a 403. This prevents anyone who discovers the API Gateway URL from posting fake Twilio data (fake opt-outs, spoofed delivery failures, etc.).

How it works: take the full request URL, append all POST params sorted alphabetically concatenated as `key1value1key2value2` (no separators), sign with HMAC-SHA1 using the Twilio auth token, base64 encode. Compare against the header in constant time.

When forwarding to Octopods we re-compute a fresh signature for the Octopods URL — the original signature Twilio sent is bound to our URL and would fail Octopods' own validation.

Debugging tip: if validation fails, CloudWatch logs print the received signature, computed signature, and param keys side-by-side. The most common cause of mismatch is a URL mismatch (check for trailing slashes, HTTP vs HTTPS, custom domain vs API Gateway domain).

### Post-deploy Twilio configuration

After first deploy, retrieve the base URL from the `SmsApiUrl` CloudFormation output and:

1. In Twilio console → Messaging Services → each of your 3 services:
   - Set **Inbound webhook URL** to `{SmsApiUrl}/sms/inbound`, method POST
2. Optionally (recommended for Phase 1 delivery visibility):
   - On outbound sends, pass `status_callback="{SmsApiUrl}/sms/status"` in the Twilio API call
   - Or set it in the Messaging Service settings if Twilio exposes that option

---

## Stripe Finance Report

**Lambda:** `gymlaunch-stripe-finance-report`  
**Schedule:** `cron(0 15 ? * MON-FRI *)` — 10am CDT (15:00 UTC). Shifts to 9am in winter CST; change `15` → `16` for 10am year-round.  
**Source:** `src/sync/stripe/finance_report/`  
**Recipient:** `SES_TO_ADDRESS` env var (default `ap@gymlaunchsecrets.com`) — change in `deploy.sh` for testing  
**Sender:** `SES_FROM_ADDRESS` env var (default `reports@gymlaunch.com`)

Pulls the most recent unprocessed paid payout from each of 3 Stripe accounts, generates a
separate CSV per account, and emails all three as attachments in one SES email.

### Stripe Accounts

| Account ID | Name |
|---|---|
| `acct_1EJ3grGXzfVc86k5` | Gymlaunch |
| `acct_1HTxq8DwroTQxWiD` | Gymlaunch Kajabi |
| `acct_1NdhHHFIzHSkCkYd` | Gym Launch Go High Level |

### Stripe API Keys (Restricted)

Stored in Secrets Manager at `gymlaunch/stripe/api_keys` as a JSON object keyed by account ID.
Each restricted key requires these Stripe permissions:

- Balance — Read
- Balance Transaction Source — Read
- Balance Transfers — Read
- Charges and Refunds — Read
- Customers — Read
- Payouts — Read

### CSV Columns

`Account, Type, ID, Created, Description, Amount, Currency, Converted Amount, Fees, Net, Converted Currency, Customer ID, Customer Email, Customer Name, GL Code, Intacct SKU`

- **Type** — from Stripe's `reporting_category` field, formatted to match dashboard display
- **ID** — source object ID; refunds use the original charge ID (`ch_`) to match Stripe's export convention; disputes/chargebacks keep their own `ad_` ID
- **Filename** — uses the payout's `arrival_date`, not the run date (e.g. `stripe_gymlaunch_2026-05-19.csv`)

### GL Code / Intacct SKU Mapping

**Per-account charge categorization:**

| Account | Match condition | Category | GL Code | SKU |
|---|---|---|---|---|
| GHL | starts with `Auto-Recharge for Sub-Account -` | auto_recharge | 40040 | GLS-MSGCRD-01 |
| GHL | starts with `Manual Recharge :` | manual_recharge | 40040 | GLS-MSGCRD-01 |
| GHL | exact: `Subscription update`, `PhoneNumberPurchase - 3DS verification`, `Add new card: 3DS verification` | fallback | 40040 | GLS-GHLAPPRS-01 |
| Kajabi | `Subscription update` + $100 or $50 | automated_fulfillment | 40050 | GLS-AUTOFUL-01MO-00100 |
| Kajabi | `Subscription update` + $200 | trainerize_1 | 40050 | GLS-TRAINERIZE-01-00 |
| Kajabi | `Subscription update` + $250 | trainerize_2 | 40050 | GLS-TRAINERIZE-01-01 |
| Gymlaunch | `Gym Launch Secrets LLC` + $27 | 1b_ads_pack | 40080 | GLS-LT-1BADSPACK-00-01 |
| Gymlaunch | `Gym Launch Secrets LLC` + $19.99 | gl_book | 40080 | GLS-LT-BOOK-00-01 |
| Gymlaunch | `Gym Launch Secrets LLC` + $192 | 192_winning_ads | 40080 | GLS-LT-192ADPACK-00-01 |

**Global row-type handling (all accounts):**

| Row type | GL Code | SKU |
|---|---|---|
| Stripe fee (`Billing - Usage Fee`, `Card payments`, `Radar`, `Card Account Updater`, `Network Tokens`) | 50080 | — |
| Refund | 48100 | Inherited from original charge |
| Dispute/Chargeback (`type=adjustment`, description starts with `Dispute` or `Chargeback`) | 48000 | Inherited from original charge |
| Payout row (`type=payout`) | — | Dropped from CSV |

Unmatched charges are logged to CloudWatch as `UNMATCHED charge` for review.

### Payout Dedup

`stripe_payout_export` table — one row per processed `payout_id`. Lambda checks this table
on every run and skips any payout already present. Rows are only written after successful
email delivery so a delivery failure can be safely retried by re-invoking the Lambda.

To re-run for a specific account (e.g. for testing):
```sql
DELETE FROM stripe_payout_export WHERE stripe_account_id = 'acct_1NdhHHFIzHSkCkYd';
```

To clear all and reprocess everything:
```sql
DELETE FROM stripe_payout_export;
```

### IAM Requirements

The `gymlaunch-slack-sync` Lambda role requires an inline policy granting `ses:SendRawEmail`.
The `gymlaunch-lambda-boundary` permissions boundary also has an `AllowSES` statement for
`ses:SendRawEmail` — both must be present for email delivery to work.

SES domain `gymlaunch.com` must be verified in us-east-1. SES must be out of sandbox mode
to send to unverified recipients.

### DST Note

Schedule fires at 15:00 UTC = 10am CDT (summer). In winter CST it fires at 9am.
To pin to 10am year-round change the cron to `cron(0 16 ? * MON-FRI *)`.

---

## HubSpot Project-Note Sync

**Lambda:** `gymlaunch-project-note-sync`
**Schedule:** `rate(72 hours)` — fires every 3 days from last deploy/fire. Drifts off clock-time, but this is a background maintenance job with no user-facing impact, so the drift is fine. Sized this way because real-world signal is sparse (~11 pairs needing writes per 30 days observed in the wild).
**Source:** `src/sync/hubspot_project_notes/`

### Why it exists

HubSpot doesn't fire a useful webhook when a note is created on the Projects object (`0-970`), so we can't intercept note creation in real time. Instead this Lambda runs nightly and back-fills associations so every note attached to a project is also associated with that project's companies and contacts. Once the note is associated to the company/contact, downstream activity rollups (and the future AI brain) see the note in context.

### API access gotcha — read this before touching the handler

The `/crm/v3/objects/0-970` list-instances endpoint (and its dated-API sibling `/crm/objects/2026-03/projects`) is gated behind a scope **not available to Private Apps** for our portal. Hitting them returns:

```
{"status":"error","message":"The scope needed for this API call isn't available for public use."}
```

This error has no `requiredScopes` array — it's not a missing-scope problem you can fix in the scope picker. It reproduces in HubSpot's own docs "Try It" widget with our token. A support ticket has been filed (correlationId `019e5159-e757-7721-818b-5f78ef50872c`).

**The v4 associations endpoints work normally** — they're how we reach Project data without listing instances. That's why this Lambda is notes-first instead of projects-first.

| Endpoint | Works for us? |
|---|---|
| `GET /crm/v3/objects/0-970` | **NO** — public-use gate |
| `GET /crm/objects/2026-03/projects` | **NO** — same gate |
| `POST /crm/v4/associations/0-970/companies/batch/read` | yes |
| `POST /crm/v4/associations/0-970/contacts/batch/read` | yes |
| `POST /crm/v4/associations/notes/0-970/batch/read` | yes |

Scopes on the Private App: `projects.read`, `custom-objects-read`, plus the existing CRM-object scopes.

### Algorithm

1. Search notes modified at or after `now - lookback_hours` (`POST /crm/v3/objects/notes/search`, sorted ascending by `hs_lastmodifieddate`, 100 per page).
2. For each batch, batch-read note→project associations (`POST /crm/v4/associations/notes/0-970/batch/read`). Drop notes not attached to any project.
3. Collect unique project IDs, batch-read each project's companies and contacts (`/crm/v4/associations/0-970/companies/batch/read` and `.../contacts/batch/read`).
4. Build candidate `(note, project)` pairs. Look up each in `hubspot_project_note_sync` — skip pairs where the snapshot matches and both sides are already marked synced.
5. For pairs needing work, batch-read each note's CURRENT note→company and note→contact associations.
6. Compute the desired set per note = UNION of every linked project's companies + contacts in this batch.
7. Batch-create missing associations (`/crm/v4/associations/notes/{companies|contacts}/batch/create/default`).
8. Upsert one state row per pair. `companies_synced_at` / `contacts_synced_at` are set to `now()` if the side is fully confirmed on the note, otherwise `NULL` so the unsynced-pair index resurfaces the pair.

### Lookback window

Default is **96 hours** (config: `DEFAULT_LOOKBACK_HOURS` in `handler.py`). Paired with the 72h schedule — 24h of overlap so a missed run doesn't drop data.

To run a one-time long backfill, invoke manually with a `lookback_hours` payload:

```bash
# 90-day backfill
aws lambda invoke \
  --function-name gymlaunch-project-note-sync \
  --invocation-type RequestResponse \
  --cli-binary-format raw-in-base64-out \
  --payload '{"lookback_hours": 2160}' \
  /tmp/out.json && cat /tmp/out.json
```

HubSpot's search API caps at 10,000 results per query. If the lookback window contains more modified notes than that, the Lambda logs a `WARNING: search total=N exceeds HubSpot's 10000 hard limit` line and processes only the oldest 10k. Tighten the window or re-invoke if you see this.

### Known gap (accepted by design)

HubSpot does **not** bump `hs_lastmodifieddate` when a note's associations change. So an old note that gets newly attached to an existing project later will **not** be caught by the modified-since search. Workflow assumption: notes are created fresh on a project, so this case is rare.

If it ever bites, the mitigation would be to cache every project ID we've seen (from any note's association) and periodically batch-read each project's notes via `/crm/v4/associations/0-970/notes/batch/read`. Not built yet.

### Behavior on edge cases

- **Note not attached to any project:** dropped at step 2. Never reaches the state table.
- **Project with no companies and no contacts:** the note has nothing to inherit on either side, so all desired sets are empty. `companies_ok` and `contacts_ok` both trivially evaluate `true` (empty subset of anything) and the state row gets both `_synced_at` timestamps set. Skipped on subsequent runs unless the project later gains parties.
- **Note on multiple projects:** associations are the UNION of every linked project's companies/contacts. A note on Project A (Acme + Joe) and Project B (Beta + Sue) gets all four associated.
- **Project loses an association:** Lambda is purely additive — it will not remove an existing association from a note.
- **Partial success (one side 429s after retry):** the side that succeeded gets `_synced_at = now()`; the failed side is set to `NULL` so `hubspot_project_note_sync_unsynced_idx` resurfaces the pair and the next run retries.

### Re-running a full backfill

To force every known pair to re-evaluate:

```sql
TRUNCATE hubspot_project_note_sync;
```

Then invoke with whatever lookback window covers the time range you care about. Note this only re-evaluates pairs that the notes search will surface — notes outside the lookback window stay un-resynced.

---

### Database schema (`hubspot_project_note_sync`)

One row per `(note_id, project_id)` pair the Lambda has touched. Composite primary key on those two columns. Migration: `db/migrations/008_project_note_sync_schema.sql`.

| Column | Type | What it means |
|---|---|---|
| `note_id` | TEXT | HubSpot Note ID, part of PK |
| `project_id` | TEXT | HubSpot Project ID (object type `0-970`), part of PK |
| `project_company_ids` | TEXT[] | Snapshot of the project's company IDs as of the last sync attempt. Used for drift detection — if the project gains a new company in HubSpot, this array won't match what the next run sees, triggering re-evaluation. |
| `project_contact_ids` | TEXT[] | Same, for the project's contact IDs |
| `companies_synced_at` | TIMESTAMPTZ NULL | When this pair's company side was last fully reconciled. NULL means it still owes work. |
| `contacts_synced_at` | TIMESTAMPTZ NULL | Same, for the contact side |
| `last_attempted_at` | TIMESTAMPTZ | When the Lambda last tried this pair |
| `last_error` | TEXT NULL | Human-readable error from the most recent attempt. NULL if last attempt succeeded. |
| `attempts` | INT | Total times we've touched this pair. Increments on every upsert. |
| `created_at`, `updated_at` | TIMESTAMPTZ | Bookkeeping |

**Partial index for retry visibility:** `hubspot_project_note_sync_unsynced_idx` on `last_attempted_at DESC WHERE companies_synced_at IS NULL OR contacts_synced_at IS NULL`. This is the "pairs that owe work" set.

A pair is considered "fully synced" iff both `companies_synced_at` and `contacts_synced_at` are non-NULL AND `project_company_ids` + `project_contact_ids` still match the live HubSpot project (snapshot drift triggers re-eval even on fully-synced rows).

---

### Operator's guide

#### How to trigger a run

**Scheduled (steady state):** EventBridge fires every 72h from the last fire/deploy. No human action needed. Uses the 96h default lookback.

**Manual one-off** (backfills, retries, debugging):

```bash
# 30-day backfill
aws lambda invoke \
  --function-name gymlaunch-project-note-sync \
  --invocation-type RequestResponse \
  --cli-binary-format raw-in-base64-out \
  --payload '{"lookback_hours": 720}' \
  /tmp/out.json && cat /tmp/out.json

# Tighter window for a debug run
aws lambda invoke \
  --function-name gymlaunch-project-note-sync \
  --invocation-type RequestResponse \
  --cli-binary-format raw-in-base64-out \
  --payload '{"lookback_hours": 1}' \
  /tmp/out.json && cat /tmp/out.json
```

`lookback_hours` only affects the single invocation it's passed to. Scheduled runs keep using 36h.

#### Common SQL queries

```sql
-- High-level health snapshot
SELECT
    count(*) FILTER (WHERE companies_synced_at IS NOT NULL AND contacts_synced_at IS NOT NULL) AS fully_synced,
    count(*) FILTER (WHERE companies_synced_at IS NULL OR contacts_synced_at IS NULL)          AS pending,
    count(*) AS total_pairs
FROM hubspot_project_note_sync;

-- Pairs that still owe work (the "must retry" set)
SELECT note_id, project_id, attempts, last_error, last_attempted_at
FROM hubspot_project_note_sync
WHERE companies_synced_at IS NULL OR contacts_synced_at IS NULL
ORDER BY last_attempted_at DESC;

-- Investigate a specific note — what does the state table say about it?
SELECT *
FROM hubspot_project_note_sync
WHERE note_id = '108879445604';

-- Investigate a specific project — every note we've reconciled to it
SELECT note_id, companies_synced_at, contacts_synced_at, last_error
FROM hubspot_project_note_sync
WHERE project_id = '522215603313'
ORDER BY last_attempted_at DESC;

-- Recently-touched pairs (e.g., after a manual invoke)
SELECT note_id, project_id, attempts, last_error
FROM hubspot_project_note_sync
WHERE last_attempted_at >= now() - interval '1 hour'
ORDER BY last_attempted_at DESC;

-- Errors grouped by kind
SELECT count(*), substring(last_error from 1 for 60) AS error_summary
FROM hubspot_project_note_sync
WHERE last_error IS NOT NULL
GROUP BY substring(last_error from 1 for 60)
ORDER BY count(*) DESC;
```

#### How to retry failed pairs

**Important nuance:** the Lambda is driven by the notes search, not by the state table's pending-pair index. A pair with NULL `_synced_at` only gets re-attempted if its underlying note still appears in the search's lookback window.

So to retry:

1. Find when the failed pairs were last attempted:
   ```sql
   SELECT MIN(last_attempted_at)
   FROM hubspot_project_note_sync
   WHERE companies_synced_at IS NULL OR contacts_synced_at IS NULL;
   ```
2. Pick a lookback that covers from then until now, in hours. E.g., if failures were 2 days ago, use `lookback_hours: 72` to be safe.
3. Manually invoke with that payload.

If the failed pairs' notes haven't been modified in a long time, you may need a very long lookback (`720` for 30 days, `2160` for 90 days). Successful pairs in the window are auto-skipped — extra lookback only costs the search-paginate calls, not the create calls.

#### How to dig into a specific note that "should" have been processed

```sql
-- Is the pair even in our state table?
SELECT * FROM hubspot_project_note_sync WHERE note_id = 'YOUR_NOTE_ID';
```

If no row → Lambda hasn't seen this note in any run yet. Either it's outside the lookback window, or it wasn't attached to a project when last scanned. Verify the note's modification date and current project association in HubSpot.

If row exists with both `_synced_at` populated → Lambda already reconciled it. The companies and contacts in `project_company_ids`/`project_contact_ids` are what got pushed onto the note. If you expected more parties, the project itself doesn't have them — fix the project's associations in HubSpot, and the next run will detect the drift and propagate.

If row exists with `_synced_at` NULL → previous run failed. Check `last_error` for the reason.

#### How to read the CloudWatch logs

```bash
aws logs tail /aws/lambda/gymlaunch-project-note-sync --follow --format short
```

The shape of a healthy run:

```
Project-note sync starting (lookback_hours=96)
Looking up notes modified at or after 2026-05-21T...
  [notes search page 1] modified_since_ms=... after=None
  [notes search page 1] 87 result(s) (running total 87/87)
    3/87 note(s) attached to a project
    fetched parties for 3 unique project(s)
    3/3 pair(s) need work (0 skipped by state table)
    creating 2 co + 1 ct association(s)
    committed: +2 co / +1 ct associations, 3 state row(s) upserted
  notes search complete after 1 page(s), 87 total
Project-note sync complete: {'lookback_hours': 96, 'notes_seen': 87, 'notes_on_project': 3,
                              'pairs_skipped': 0, 'pairs_worked': 3, 'company_assoc_made': 2,
                              'contact_assoc_made': 1, 'errors': 0}
```

Things to watch for:

| Log line | What it means |
|---|---|
| `Another project-note sync is running, exiting` | Two invocations overlapped; the second yielded to the advisory lock. Harmless. |
| `WARNING: search total=N exceeds HubSpot's 10000 hard limit` | The lookback window has too many notes. Tighten the window and re-invoke. |
| `Rate limited by HubSpot, waiting Ns and retrying` | One HTTP retry happened. Fine, just means HubSpot was busy. |
| `Batch create notes→{type} failed: NNN ...` | A batch-create call failed. If HTML 404, the endpoint URL is wrong. If JSON 429 after retry, rate limit. If MISSING_SCOPES, scope on Private App changed. |
| `N co association(s) FAILED: [...]` | Specific pairs that failed. Their state rows get NULL on the failed side and surface in the pending-pair query above. |

#### Reset scenarios

| Goal | Action |
|---|---|
| Retry just the failed pairs | Manual invoke with `lookback_hours` covering when failures originated |
| Re-sync everything currently in scope | `TRUNCATE hubspot_project_note_sync;` + manual invoke with desired window |
| Pause the nightly schedule | `aws events disable-rule --name gymlaunch-project-note-sync-nightly` |
| Resume the nightly schedule | `aws events enable-rule --name gymlaunch-project-note-sync-nightly` |
| Delete a specific bad row | `DELETE FROM hubspot_project_note_sync WHERE note_id = 'X' AND project_id = 'Y';` then re-invoke with a window that catches that note |

#### Why this Lambda exists in context

The long-term goal is a unified customer activity layer for AI insights — a "brain" that knows every SMS, call, Slack message, Asana task, and HubSpot interaction tied to a given client. For that to work, every note on a project has to be reachable from the company/contact's activity feed. HubSpot's UI shows project-notes on the project, but they're invisible at the company/contact level unless explicitly associated — which is what this Lambda fixes.

Without this Lambda, the AI brain would have a blind spot for any context captured as a project-note. With it, project-notes propagate to the related company and contact automatically (with up to 24h latency from the nightly cron).

### Inspecting state

```sql
-- Pairs that still owe work
SELECT note_id, project_id, attempts, last_error
FROM hubspot_project_note_sync
WHERE companies_synced_at IS NULL OR contacts_synced_at IS NULL
ORDER BY last_attempted_at DESC;

-- How many pairs we've reconciled overall
SELECT count(*) FILTER (WHERE companies_synced_at IS NOT NULL AND contacts_synced_at IS NOT NULL) AS fully_synced,
       count(*) FILTER (WHERE companies_synced_at IS NULL OR contacts_synced_at IS NULL) AS pending
FROM hubspot_project_note_sync;
```

### Advisory lock

Uses `pg_try_advisory_lock(hashtext('project_note_sync'))` to prevent overlapping runs, matching the pattern in the Asana and HubSpot syncs.

---

## Fathom

### gymlaunch-fathom-daily-sync

Nightly sync against the Fathom API. Pulls meetings and upserts into `fathom_call` + `fathom_call_invitee`.

**Normal run (scheduled):** fetches meetings created in the last 48 hours using the `created_after` param.
Skips meetings already in DB with a non-null `transcript_plaintext`.

**Full sync:** set `FULL_SYNC=true` in the Lambda console env vars, then invoke manually (or wait for the
scheduled run). Fetches ALL meetings with no date filter, skipping ones already fully stored. Set back
to `false` afterward (a deploy also resets it).

Both runs use `include_transcript=true` on the `/meetings` list endpoint — transcripts come back inline,
no separate per-meeting API calls needed. Upsert uses `COALESCE` on `transcript_plaintext` so an existing
transcript is never overwritten with NULL.

API: `GET https://api.fathom.ai/external/v1/meetings` — cursor-based pagination (`next_cursor` / `cursor`),
meetings wrapped in `items`, 60 req/min rate limit.
API key: `gymlaunch/fathom/Fathom-API-Key` (key: `api_key`), passed as `X-Api-Key` header.

**To manually invoke (e.g. for a backfill):**
```bash
aws lambda invoke \
  --function-name gymlaunch-fathom-daily-sync \
  --invocation-type RequestResponse \
  --cli-binary-format raw-in-base64-out \
  --payload '{}' \
  /tmp/out.json && cat /tmp/out.json
```

**First run (full backfill):**
1. Set `FULL_SYNC=true` in the Lambda console env vars
2. Invoke manually (or wait for the 4am UTC schedule)
3. Set `FULL_SYNC` back to `false`

### gymlaunch-fathom-webhook

Receives call transcript payloads fired by Zapier after each Fathom-recorded call.
Validates an `X-Webhook-Secret` header on every request (constant-time compare). Returns 403
on mismatch, 400 if `fathom_id` is missing, 200 on success, 500 on DB error.

Writes to two tables:
- `fathom_call` — upserted on `fathom_id`. `summary` column is left NULL; filled later by AI pipeline.
- `fathom_call_invitee` — upserted on `(call_id, email)`. Primary invitee comes from the flat
  `meeting_invitees_email` / `meeting_invitees_name` / `meeting_invitees_is_external` fields.
  Additional invitees from `meeting_invitees` array are parsed if present.

**Zapier setup:** point the webhook action at `{SmsApiUrl}/fathom/webhook`, method POST,
body type JSON. Map all Fathom fields directly. Add a custom header:
`X-Webhook-Secret: <value from gymlaunch/fathom/Fathom-Webhook-Secret>`.

**AI brain query pattern** — all calls involving a contact:
```sql
SELECT fc.*
FROM fathom_call fc
JOIN fathom_call_invitee fci ON fci.call_id = fc.id
WHERE fci.email = 'john@example.com'
ORDER BY fc.meeting_scheduled_start_time DESC;
```

See `src/fathom/webhook/how_to_query.txt` for full query reference.

---

## SubscriptionFlow

Three independent Lambdas. **Full narrative: `docs/subscriptionflow_explained.md`.**
**Secret (shared):** `gymlaunch/subscriptionflow/api` — JSON `client_id`, `client_secret`, `endpoint_api_key`.
**Auth (shared):** OAuth2 client-credentials; bearer token cached in `subscriptionflow_oauth_token`
(singleton, migration 013) and rotated in-band (proactive on expiry, reactive on 401).

**Tables:** `subscriptionflow_oauth_token` (013); `sf_customer`/`sf_subscription`/`sf_invoice`/
`sf_transaction`/`sf_product` (015, the data lake — TEXT PKs, `raw jsonb`, no hard FKs);
`sf_sync_state` (016, sync cursor); `sf_hubspot_push_state` (017, push change-detection hashes).

### 1. `gymlaunch-sf-create-custom-weekly-sub-for-go-product` — subscribe endpoint
`src/subscriptionflow/create_sub/` · Function URL (on-demand) — **manually managed** (deploy user
can't create Function URLs). Auth: `x-api-key` header or `?key=` matching `endpoint_api_key`.
Finds/creates the SF customer, then creates a **1-year Termed** sub (`is_auto_renew:0` → ends after
a year). `pay_invoice` OFF (invoice left DUE). `price` defaults to 0.00. Weekly cadence comes from the
SF plan config, NOT the payload. `DEBUG=1` = read-only dry run returning the body it would post.
**Triggered by:** the "agreements" workflow → PandaDoc agreement (template `eqaZLY8q6WWBoKiXrXtPcZ`)
→ on signing, **Zapier zap** `https://zapier.com/editor/369877019/published` calls this Function URL.
Edit that Zap to change what's sent.

### 2. `gymlaunch-subscriptionflow-daily-sync` — SF → RDS
`src/subscriptionflow/sync/` · `cron(30 6 * * ? *)` (06:30 UTC). Mirrors 5 SF objects into the `sf_*`
tables. **Backfill** = `GET /<obj>/with-relations` (page-based); **incremental** = `POST /<obj>/filter`
with `filter[updated_at][$gte]` (the plain list IGNORES that filter — hence /filter). Per-page upsert,
**resumable** via `sf_sync_state` (checkpoints `backfill_next_page`, stops ~60s before deadline), then
incremental once `backfill_done`. `DEBUG=1` = one-page sample dry run; `FULL_SYNC=1` = force re-backfill
(one run then off). Nested fields (`primary_*_id`, `plan_id`, `billing_frequency`) are NULL — deferred
(with-relations omits `items[]`); `raw jsonb` has everything. Monitor: `SELECT * FROM sf_sync_state`.

### 3. `gymlaunch-sync-sf-billing-info-to-hubspot` — RDS → HubSpot company fields
`src/subscriptionflow/hubspot_push/` · `cron(45 6 * * ? *)` (06:45 UTC, after the sync). One SQL rollup
computes 6 company properties per `sf_customer.hubspot_id`, batch-updates HubSpot **companies** (100/call),
pushing only CHANGED companies (md5 vs `sf_hubspot_push_state`). Billing-active companies only; non-numeric
hubspot_ids skipped. Properties: `billing_status` (dropdown Current/Past Due/Cancelled/Pending),
`outstanding_balance`, `current_billing_amount`, `billing_frequency`, `last_payment_date`, `next_payment_date`.
Product/frequency are Path A text-parse (product_id not synced). `DEBUG=1` = compute + sample, no writes.

**Watch-outs / known gaps:** `docs/future_work.md` (price hardening, plan registry, `billing_frequency`
blank for prepaid-annual, real product categorization, churn scoring, failed-payment alerting).

---

## Supabase Lead Sync

**Lambda:** `gymlaunch-supabase-lead-sync`
**Schedule:** `cron(0 8 * * ? *)` — daily at 08:00 UTC = 2am CST (winter) / 3am CDT (summer).
**Source:** `src/sync/supabase_leads/`
**Secret:** `gymlaunch/supabase/api` — JSON with keys `url` and `service_role_key`.

### Why it exists

Two upstream webhook flows (Facebook Lead Ads → n8n, GHL Contact-Created → n8n) write into a Supabase project, one row per lead per side. The expectation is that the two sides match per-client per-day; mismatches are an integration break. Phase 1 (this Lambda) mirrors the Supabase tables to RDS so we can query them with the rest of the data lake. Phase 2 (deferred) adds a comparison view + a Google Sheet output for daily monitoring.

### Connection method

REST API (PostgREST) using the Supabase `service_role` JWT, **not** direct Postgres. The Supabase DB password is suspected to be hardcoded in an n8n workflow upstream, and rotating it would risk breaking that workflow. The service_role JWT is a separate auth mechanism.

### Tables mirrored

| Supabase source | RDS destination | Role |
|---|---|---|
| `02 - Facebook Leads` | `supabase_facebook_lead` | Active — compare side A in phase 2 |
| `03 - HighLevel Leads` | `supabase_highlevel_lead` | Active — compare side B in phase 2 |
| `01 - Lead Form Database` | `supabase_lead_form` | Active — small reference |
| `lead_forms` | `supabase_lead_form_detail` | Active — richer form metadata |
| `Facebook Form Database` | `supabase_facebook_form` | Legacy |
| `Facebook Lead Form Database` | `supabase_facebook_lead_form` | Legacy, near-empty |
| `Facebook Leads Database` | `supabase_facebook_lead_legacy` | Legacy, predecessor of `02 - Facebook Leads` |

**Not mirrored:** `00 - Lead Integration Main Database` — the same per-client metadata is already captured in `asana_agency_board_task` (gym_name, client_name, facebook_page_id, facebook_ad_account_id, hl_sub_account_location_id, hubspot_company_id). The Supabase Main table also stored GHL API keys in plaintext, which we deliberately did not propagate.

### Sync strategy

Full pull + upsert by PK every night. The original ask was a 7-day rolling window, but the `created_at` column in the lead tables is stored as M/D/YYYY *text* which doesn't sort lexically — so a clean server-side range filter isn't possible. Pulling everything is the simplest correct alternative and fits well within Lambda budget at current scale (~70k rows × 2 lead tables ≈ ~1 minute total wall time).

The lead tables parse `created_at` text into a real `DATE` column called `lead_date`, and keep the original string in `created_at_raw` for forensic debugging.

### Failure mode

Per-table failures are isolated — a 500 from Supabase or a constraint violation on one table does NOT abort the other six. Each table's outcome is written to `supabase_sync_state`. If ANY table failed, the Lambda returns `statusCode: 500` so a CloudWatch alarm can flag the run.

### Common SQL

```sql
-- Last sync health
SELECT table_name, last_status, last_synced_at, last_row_count, last_error
FROM supabase_sync_state
ORDER BY last_synced_at DESC;

-- Per-client per-day counts on both sides (phase-2 preview)
SELECT
    f.sub_account_id,
    f.sub_account_name,
    f.lead_date,
    COUNT(DISTINCT f.lead_id) AS fb_count,
    COUNT(DISTINCT h.lead_id) AS hl_count
FROM supabase_facebook_lead f
FULL OUTER JOIN supabase_highlevel_lead h
    ON f.sub_account_id = h.sub_account_id
   AND f.lead_date      = h.lead_date
GROUP BY 1, 2, 3
ORDER BY 3 DESC, 1;
```

### Phase 2 (not yet built)

Compare view (FB vs GHL per client/day), Google Sheet write to `1TR2SQxtmawOat-VXiuOqo94vsMyLEjLsnjeK4ygNwqs` (tab `Status`), and integration-break threshold logic. Sheet is shared with the same service account used by `gymlaunch-mb-capacity-sheet-sync` (`n8n-sheets-integration@zsign-transfer.iam.gserviceaccount.com`).

Open questions blocking phase 2:
- Threshold for "Integration Break" vs "In Sync" (strict equality vs tolerance of N)
- Whether to preserve the "system user" / "non-system user" distinction from the old workflow
- How to display all-zero-on-both-sides days (In Sync vs No Activity vs filter out)

---

## Lead-DB2 Sheet Sync

**Lambda:** `gymlaunch-lead_db2-sheet-sync`
**Schedule:** `cron(0 7 * * ? *)` — daily at 07:00 UTC (1 hour before `gymlaunch-supabase-lead-sync`).
**Source:** `src/sync/lead_db2_sheet/`
**Sheet:** `Lead Integration Database V2.0`, tab `00 - Database` (sheet ID `1JGdbjR1g8MF0zzraOwyPNNY7-jRrVIzk2ZZI-FWhky0`).
**RDS destination:** `client_lead_master`.

### Why it exists

The `00 - Database` tab is the operational client master for the FB-lead pipeline. Every client (FB page ↔ GHL location pairing) has one row in it. It carries:

1. **Auto-populated fields** — written by the n8n workflow `00 - Main Workflow` (the FB leadgen webhook receiver) on every lead arrival, via `appendOrUpdate` keyed on `facebook_page_id`. Covers the 12 identifier and status columns.
2. **Manually-maintained diagnostic fields** — 10 columns the team edits by hand: `system_user` (Yes/No), `fb_app_status`, `ghl_connection`, `workflow_connection`, `page_connection`, `lead_access_issue`, `ghl_snapshot`, `notes`, `lead_forms`, `supabase`.

The phase-2 compare sheet needs `system_user` (and likely other diagnostic flags) per row. Nothing else in our data lake carries that data — it lives only in this sheet. So we mirror the whole sheet daily into `client_lead_master` and let humans keep editing the sheet exactly as before.

**Column rename note:** the sheet's `system_user` column maps to `is_system_user` in RDS, because `SYSTEM_USER` became a reserved keyword in PostgreSQL 16 (SQL:2023 — returns the authenticated session user). The Lambda handles the rename when ingesting. Sheet column names are unchanged from the team's perspective.

### What gets synced

All 22 sheet columns. `hl_sub_account_api_key` is mirrored as plaintext as of migration 011 — needed for future per-client GHL automation. Hardening to Secrets Manager is tracked in `docs/future_work.md` under "Harden GHL API key storage."

### Sheet access

The Google service account `n8n-sheets-integration@zsign-transfer.iam.gserviceaccount.com` must have at least Viewer access on the sheet. If sync starts returning 403s, check the share list. Same service account already has Editor on the MB capacity sheet.

### Failure mode

If the sheet's header row gets renamed, the Lambda fails LOUDLY (`RuntimeError: Sheet is missing expected header columns: [...]`) rather than silently writing NULLs. State row in `supabase_sync_state` marks `last_status='error'` with the diff so an operator can spot it.

### Common SQL

```sql
-- Counts by is_system_user / asana_status combo
SELECT is_system_user, asana_status, COUNT(*)
FROM client_lead_master
GROUP BY 1, 2
ORDER BY 1, 2;

-- Clients flagged as non-system-user candidates (fb_app_status = 'Error')
SELECT client_name, gym_name, fb_app_status, is_system_user, asana_status
FROM client_lead_master
WHERE fb_app_status = 'Error'
ORDER BY client_name;

-- Last sync result
SELECT last_synced_at, last_row_count, last_status, last_error
FROM supabase_sync_state
WHERE table_name = 'client_lead_master';
```

---

## AI Brain — Client Identity & Pulse

Phase 1 of the unified client-activity layer. Two Lambdas turn the long-dormant
identity tables (`client_account` / `client_external_id`, from migration 001)
into the "address book" that ties a client's Slack, Asana, and Fathom activity
to one canonical record, then summarizes it.

### gymlaunch-client-identity-resolver (`src/identity/resolver/`)

Daily, idempotent. The **first writer** to `client_account` / `client_external_id`.

How each link is derived:
- **asana** — `asana_agency_board_task` top-level cards with a `gym_name`:
  `task_gid → (asana, task)`, `hubspot_company_id → (hubspot, company)`.
- **slack** — **Tier-0 HubSpot read** (no mirror, nothing persisted from HubSpot
  except the resulting link). Batch-reads the HubSpot company property holding the
  Slack channel id for the companies we already track, via the same
  `/crm/v3/objects/companies/batch/read` pattern as the coach sync. The property
  internal name is auto-discovered from `/crm/v3/properties/companies` (logged),
  or pinned via `SLACK_CHANNEL_PROPERTY`. `channel id → (slack, channel)` and sets
  `slack_channel.client_account_id`.
- **fathom** — derived from Slack: external (`is_internal=false`) posters in a
  linked channel → their `slack_user.email` → `(fathom, email)`.

**Owner grouping:** union-find merges cards sharing the same HubSpot channel value
(primary) or the same `client_name` (fallback), so an owner's locations collapse
into one `client_account`.

**Churn:** a client with no live (uncompleted/still-present) card is set
`active = false` (history kept); reappearing reactivates it.

Env: `HUBSPOT_TOKEN`, optional `SLACK_CHANNEL_PROPERTY`, optional
`DEFAULT_ORGANIZATION_ID`. Advisory-locked against concurrent runs.

### gymlaunch-client-pulse-summary (`src/pulse/summary/`)

Every 14 days. For each allowlisted, `active` client it gathers the window's
Slack (via the `ai_readable_messages` view — sensitivity-filtered), Asana
(tasks + comments for the client's `task` gids and their subtasks), and Fathom
(`fathom_call` joined to `fathom_call_invitee.email`), resolved through
`client_external_id`. It sends the bundle to Claude (`claude-opus-4-8`, adaptive
thinking) and upserts the result into `client_period_summary` with
`source_counts` + `model`. No delivery yet — query the table.

Env: `ANTHROPIC_API_KEY`, `ALLOWLIST_CLIENT_ACCOUNT_IDS` (comma-separated test
client ids), optional `WINDOW_DAYS` (14), `PULSE_MODEL` (`claude-opus-4-8`).

```sql
-- Latest pulse per client
SELECT ca.name, s.period_start, s.period_end, s.source_counts, s.body
FROM client_period_summary s
JOIN client_account ca ON ca.id = s.client_account_id
ORDER BY s.period_end DESC, ca.name;

-- A client's resolved address book
SELECT system, id_type, value FROM client_external_id
WHERE client_account_id = :id ORDER BY system, id_type;
```

---

## Future Work

Deferred work lives in [`docs/future_work.md`](future_work.md) — a per-entry
list with capture date, why we deferred, and the trigger condition for picking
it back up. Currently tracked there:

- **Replace the n8n FB-lead pipeline** — bringing the FB → GHL forwarder
  on-platform. The upstream `00 - Main Workflow` is load-bearing, not just a
  monitor; replacing it is a multi-Lambda migration. Phase-2 compare doubles
  as the safety net for this.
- **Harden GHL API key storage** — currently plaintext in
  `client_lead_master.hl_sub_account_api_key`. Move to Secrets Manager
  (Option B2) when team grows or when `pg_dump` exposure becomes a concern.

---

## Orphaned Resources (cleanup needed)

- **`gymlaunch-sheets-sync`** Lambda — was renamed to `gymlaunch-mb-capacity-sheet-sync`.
  The old function still exists in AWS but is no longer in the stack or on any schedule.
  Delete it from the Lambda console.
