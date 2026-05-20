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
| `gymlaunch-mb-capacity-sheet-sync` | 8am, 12pm, 4pm, 8pm EDT | `src/sync/sheets/` |
| `gymlaunch-sms-interceptor` | HTTP webhook (API Gateway) | `src/sms/interceptor/` |
| `gymlaunch-phone-validator` | HTTP webhook (API Gateway) | `src/phone/validator/` |
| `gymlaunch-stripe-finance-report` | M-F at 10am Central (15:00 UTC) | `src/sync/stripe/finance_report/` |

All functions run in the VPC (subnets `subnet-a085c381`, `subnet-3d566b33`) so they can reach RDS.  
All share IAM role `gymlaunch-slack-sync` (role named after first Lambda — same naming quirk as stack).  
Permissions boundary: `gymlaunch-lambda-boundary`.

### CloudWatch Log Groups

```
/aws/lambda/gymlaunch-slack-sync
/aws/lambda/gymlaunch-asana-agency-board-sync
/aws/lambda/gymlaunch-asana-agency-board-deep-sync
/aws/lambda/gymlaunch-sync-agency-board-to-hubspot
/aws/lambda/gymlaunch-mb-capacity-sheet-sync
/aws/lambda/gymlaunch-sms-interceptor
/aws/lambda/gymlaunch-phone-validator
/aws/lambda/gymlaunch-stripe-finance-report
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

### Key Views

| View | Purpose |
|---|---|
| `staff_availability` | Live join of capacity vs active client count per media buyer. Columns: `name`, `role`, `capacity`, `active_count`, `free_slots`. |

The view is a live query — no refresh needed. Active count is derived from
`asana_agency_board_task` filtered to active sections with a real media buyer assigned.

Query reference: `db/how_to_query.txt`

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

- `gymlaunch-hubspot-sync` — runs at :10 past every hour
- Reads rows from `asana_agency_board_task` where `content_hash != last_synced_hash`
- Pushes changes to HubSpot via Batch Companies API (`/crm/v3/objects/companies/batch/update`)
- Special case: when `coach = "Agency Pro"`, clears the field via a separate PATCH
- Account manager mapping via `account_manager_hubspot_map` table

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

### gymlaunch-sms-interceptor

Validates the Twilio HMAC-SHA1 signature on every request, writes to RDS, runs opt-out /
delivery-failure logic, then forwards the raw body to Octopods.

**Opt-out handling** (`/sms/inbound`): if the message body exactly matches a CTIA keyword
(STOP, STOPALL, UNSUBSCRIBE, CANCEL, END, QUIT), the Lambda searches HubSpot for the
sender's phone number (mobilephone first, phone as fallback, multiple format variants in
one API call) and sets `sms_opted_out = true` on the contact.

**Delivery failure handling** (`/sms/status`): if `ErrorCode` is in `{30006, 30007, 30008}`,
the Lambda finds the recipient's HubSpot contact and sets `sms_deliverable = false` plus
`sms_ineligible_reason` to the mapped reason string.

**HubSpot properties required** (must be created manually in portal 43776308):

| Property | Type | Set by |
|---|---|---|
| `sms_subscriptions` | Multiple checkbox | Enrollment state per channel (options: Marketing, Product Updates, Support) |
| `sms_marketing_opted_out` | Checkbox | STOP/START on Marketing channel |
| `sms_product_updates_opted_out` | Checkbox | STOP/START on Product Updates channel |
| `sms_marketing_opted_out_date` | Date | Set on Marketing STOP; not cleared on START |
| `sms_deliverable` | Checkbox | Hard delivery failure (30006/30007/30008) |
| `sms_ineligible_reason` | Single-line text | `geo_block` / `carrier_violation` / `carrier_error` |

The `sms_subscriptions` internal option names must match the channel names in `_CHANNEL_PROPS` in `handler.py` exactly (case-sensitive).

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

## Orphaned Resources (cleanup needed)

- **`gymlaunch-sheets-sync`** Lambda — was renamed to `gymlaunch-mb-capacity-sheet-sync`.
  The old function still exists in AWS but is no longer in the stack or on any schedule.
  Delete it from the Lambda console.
