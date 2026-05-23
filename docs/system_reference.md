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
| `gymlaunch-project-note-sync` | Every 72 hours | `src/sync/hubspot_project_notes/` |

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
/aws/lambda/gymlaunch-project-note-sync
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
| `db/migrations/007_fathom_schema.sql` | `fathom_call`, `fathom_call_invitee` tables |
| `db/migrations/008_project_note_sync_schema.sql` | `hubspot_project_note_sync` table |

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

## Orphaned Resources (cleanup needed)

- **`gymlaunch-sheets-sync`** Lambda — was renamed to `gymlaunch-mb-capacity-sheet-sync`.
  The old function still exists in AWS but is no longer in the stack or on any schedule.
  Delete it from the Lambda console.
