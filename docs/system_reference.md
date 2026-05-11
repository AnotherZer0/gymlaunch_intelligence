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

### Key Tables

| Table | Purpose |
|---|---|
| `asana_task` | All Asana tasks (raw) |
| `asana_user` | Asana users |
| `asana_agency_board_task` | Denormalized agency board rows with custom fields |
| `agency_staff_capacity` | Media buyer names + capacity limits |
| `account_manager_hubspot_map` | AM name → HubSpot company owner ID |

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

## Orphaned Resources (cleanup needed)

- **`gymlaunch-sheets-sync`** Lambda — was renamed to `gymlaunch-mb-capacity-sheet-sync`.
  The old function still exists in AWS but is no longer in the stack or on any schedule.
  Delete it from the Lambda console.
