# Future Work

A running list of intentionally-deferred work for the GymLaunch Intelligence
data lake. Each entry captures what to do, why we deferred it, and what would
trigger picking it back up.

**When to add an entry:** every time a design decision deliberately defers a
piece of work, a "we should do X eventually" thought emerges mid-conversation,
or a "future-you will thank you" note comes up during a review. Better to
overcapture than to lose context across sessions.

**When picking work back up:** scan this file first. Look for entries whose
"revisit when" condition is now true. If you're starting a new session on a
topic that touches a deferred area, surface the relevant entry in your first
response so the user remembers the context.

**Status tags:**
- `[open]` — deferred, not started
- `[in-progress]` — being worked on right now
- `[done]` — completed; left here for context and to record when it landed
- `[abandoned]` — explicitly decided not to do (with reasoning)

---

## [open] HubSpot Tier-1 read-only company mirror

**Captured:** 2026-06-26

**Revisit when:** the AI brain needs HubSpot context beyond the Slack-channel
property — e.g. contact emails as a more reliable Fathom key than Slack posters,
deal/lifecycle stage, or owner attribution — OR the identity resolver's Slack
linking proves too thin (companies without the channel property filled in).

**Why it matters:**
Phase 1 (`gymlaunch-client-identity-resolver`) deliberately uses **Tier 0**: an
on-demand batch-read of the single HubSpot company Slack-channel property,
persisting nothing from HubSpot except the resulting `client_external_id` link.
That's enough to wire Slack + Fathom to a client, but the brain will eventually
want richer HubSpot data in the DB (the memory note calls a `hubspot_contact`
table the eventual "identity spine").

**Agreed scope when we build it (Tier 1, NOT Tier 2):**
- One narrow, **read-only** mirror table `hubspot_company` (`company_id` PK,
  `name`, `owner_id`, `slack_channel`, `hs_lastmodifieddate`, `raw_payload`
  jsonb, `synced_at`); later `hubspot_contact`.
- **Incremental pull, no webhooks:** CRM Search filtered by
  `hs_lastmodifieddate >= last_run` + a cursor/state row — the same
  recently-modified-lookback pattern as `gymlaunch-project-note-sync`.
- **Bounded to agency-client companies** (the IDs we already track via
  `asana_agency_board_task.hubspot_company_id`), not the whole portal.
- **Read-only inbound only.** The existing push sync
  (`gymlaunch-sync-agency-board-to-hubspot`) stays separate — no bidirectional
  write-conflict surface.
- Reuses the existing `gymlaunch/hubspot/token` Private App credential.

**Explicitly out of scope:** Tier 2 — a full bidirectional CRM mirror of all
objects with real-time webhooks. Not planned.

---

## [open] Phone validator rewrite + HubSpot workflow wiring

**Captured:** 2026-05-22

**Revisit when:** user has decided what to do when no usable numbers are found
(neither phone nor mobile can be parsed). Everything else is spec'd and ready to write.

**Why it matters:**
The current `src/phone/validator/handler.py` is deployed and the endpoint is live,
but the logic doesn't match the actual requirements. It was designed as a combined
phone+mobile normalizer but the two fields serve completely different purposes.
Do NOT wire it to HubSpot until the rewrite is done.

**Open question (blocking):**
What to do when `mode: "mobile"` runs and mobile cannot be parsed at all?
Options: flag the contact with a HubSpot property, create a note only, do nothing silently.
User was deciding — ask them before writing any code.

**Agreed design — two modes in one Lambda:**

`mode: "mobile"` (SMS use case — primary)
- Normalize mobilephone to E.164, run Twilio Lookup on mobile only
- Country allowlist: US, CA, GB, JE, IM, BS, PR, AU, NZ
- Line type suppression: suppress `landline` and `voip`; allow `mobile`, `nonFixedVoip`, others
- If already E.164 — still run Lookup, just don't flag for update
- NEVER touch phone field
- Return `note_body` as pre-formatted string (workflow creates the note)

`mode: "phone"` (power dialer use case — secondary)
- Normalize phone only, NO Twilio Lookup
- Return validity flag
- If phone invalid + mobile valid → return `new_phone_value` = mobile E.164 (workflow writes it)
- NEVER touch mobilephone field

**Two HubSpot workflows to build after rewrite:**
1. Mobile validation — trigger: mobilephone updated (filter: last 1 day). Branch on
   `singular_route`. Set mobilephone if needed. Create note from `note_body`.
2. Phone shape check — trigger: list membership (power dialer list). If phone invalid
   and new_phone_value present → set phone. Create note.

**What to build:**
1. Confirm no-usable-numbers handling with user
2. Rewrite `src/phone/validator/handler.py` with two-mode logic
3. Redeploy (`bash scripts/deploy.sh`)
4. Build Workflow 1 in HubSpot (mobile validation)
5. Build Workflow 2 in HubSpot (phone shape / power dialer)

---

## [in-progress] Fathom nightly API sync

**Captured:** 2026-05-16
**Started:** 2026-06-24

**Status:** Code written (2026-06-24). Lambda `gymlaunch-fathom-daily-sync`.
Schedule: `cron(0 4 * * ? *)` — 4am UTC (11pm CDT).
**Needs deploy:** `bash scripts/deploy.sh`, then set `FULL_SYNC=true` in Lambda console + invoke for first backfill.

**Why it matters:**
Replacing the webhook (Zapier → `gymlaunch-fathom-webhook`) as the primary data source.
Webhook stays alive as a low-cost insurance policy. Nightly sync via Fathom API is more
reliable and removes Zapier as a dependency.

**Design:**
- Normal run: `GET /meetings?after=48h_ago&include_transcript=true`
- Full sync: `FULL_SYNC=true` env var → paginate ALL meetings → batch-check IDs against DB
  → only fetch transcripts for meetings NOT already in DB (avoids redundant API calls)
- Upsert on `fathom_id` — idempotent
- Code: `src/fathom/sync/handler.py`
- Fathom API key: `gymlaunch/fathom/Fathom-API-Key` (key: `api_key`), `X-Api-Key` header
- Rate limit: 60 req/min

---

## [abandoned] Fathom summary PATCH endpoint

**Captured:** 2026-05-16
**Abandoned:** 2026-06-24

**Why abandoned:**
The summary pipeline (n8n → Claude API → PATCH → DB) was designed when we thought
we needed summaries to control token cost at AI brain query time. Decided against it
because: (1) Claude's 200k context window handles 15–20 full call transcripts
comfortably; (2) the extra infrastructure (n8n workflow, PATCH endpoint, second API
call per call) isn't worth it at current call volume; (3) full transcripts give the
AI brain richer context without a lossy summarization step.

The `fathom_call.summary` column stays NULL and can be dropped in a future migration
if it gets in the way. Do not build this endpoint.

---

## [abandoned] Fathom summary pipeline + two-tier retrieval design

**Captured:** 2026-05-16
**Abandoned:** 2026-06-24

**Why abandoned:** see "Fathom summary PATCH endpoint" entry above. Full transcripts
are used directly by the AI brain. The `summary` column stays NULL.

---

## [open] Replace the n8n FB-lead pipeline

**Captured:** 2026-05-29

**Revisit when:** team commits to the migration, OR n8n has an outage that
causes customer-impacting lead loss, OR Supabase project is being torn down for
cost/security reasons.

**Why it matters:**
The upstream n8n workflow `00 - Main Workflow` is the load-bearing FB-lead →
GHL forwarder, not a monitoring workflow. If it dies, customers stop receiving
leads in their CRM. The entire `00 - Database` sheet, the Supabase
`02 - Facebook Leads` / `03 - HighLevel Leads` tables, and our RDS mirrors all
depend on it. Replacing it brings the path on-platform with the same
resilience model as the SMS pipeline.

**What to build:**
1. **`gymlaunch-fb-lead-webhook`** — a Lambda webhook receiver at
   `POST /fb/leadgen` on the existing `gymlaunch-intelligence` API Gateway.
   Validates Meta's `X-Hub-Signature-256` HMAC, writes the lead directly to RDS,
   calls the GHL API to create the corresponding contact using each client's
   per-location API key.
2. **`gymlaunch-ghl-contact-webhook`** — Lambda at `POST /ghl/contact`.
   Validates a configured shared secret, writes the contact event directly to
   RDS.
3. **Re-subscribe each FB page** (~700) to OUR webhook URL instead of n8n's.
   One-time script using the existing FB System User app.
4. **Update each GHL location's outbound webhook config** to point at our
   endpoint. Tedious but scriptable via the GHL API.
5. **Run in parallel** with the n8n workflow for 1-2 weeks. Reconcile RDS
   counts vs Supabase counts. Once parity is established, disable the n8n
   workflow and eventually drop the Supabase project.

**Cascade effects when this lands:**
- `gymlaunch-supabase-lead-sync` becomes obsolete (remove it from the stack).
- `gymlaunch-lead_db2-sheet-sync` may stay (if the sheet survives as a manual
  diagnostic UI) or pivot to syncing FROM RDS BACK TO a sheet so the team
  retains a familiar interface.
- The phase-2 compare sheet doubles as the safety net during the migration —
  any drift between FB count and GHL count after cutover surfaces immediately.

---

## [open] HubSpot Projects API — support ticket follow-up

**Captured:** 2026-05-29

**Revisit when:** HubSpot support responds to ticket `correlationId 019e5159-e757-7721-818b-5f78ef50872c`
(portal 43776308) granting access to the Projects list-instances endpoint, OR
HubSpot announces general availability of the `/crm/v3/objects/0-970` endpoint.

**Why it matters:**
`gymlaunch-project-note-sync` uses a notes-first design as a workaround because
`/crm/v3/objects/0-970` (and `/crm/objects/2026-03/projects`) returns
"scope isn't available for public use" for all Private Apps — including with
`projects.read` and `custom-objects-read` scopes. The v4 association endpoints
work normally; we use them to sidestep the gate.

If projects listing is ever granted, a projects-first design would close the
known coverage gap (see next entry) — we could iterate all projects, not just
recently-modified notes.

**What to build when revisiting:**
1. Test `GET /crm/v3/objects/0-970` — if it returns data, the gate is lifted
2. Rewrite `src/sync/hubspot_project_notes/handler.py` to projects-first:
   iterate all projects, fetch their notes and associated parties, diff vs state
3. Remove the `hs_lastmodifieddate` lookback window — no longer needed once
   we can enumerate projects directly
4. Consider archiving the `hubspot_project_note_sync` state table or pivoting its
   PK to `(project_id, object_id, object_type)` for the new design

---

## [open] Project-note sync coverage gap — old note newly attached to project

**Captured:** 2026-05-29

**Revisit when:** a real-world case surfaces where a note was created before a
project existed (or created outside a project, then manually attached later),
and that note's associations never get propagated.

**Why it matters:**
HubSpot does NOT bump `hs_lastmodifieddate` when a note's associations change.
So if someone creates a note outside a project today, then attaches it to a
project tomorrow, the modified-since search used by `gymlaunch-project-note-sync`
will never surface that note. The gap is accepted for now because the team's
workflow is to create notes directly inside projects — the retroactive-attach
case is rare and hasn't been observed in production yet.

**Mitigation to build if the gap becomes real:**
1. Accumulate all `project_id` values we've seen in `hubspot_project_note_sync`
   (they're already in the state table)
2. Add a periodic (e.g., monthly) pass that reads each project's notes via
   `/crm/v4/associations/0-970/notes/batch/read` — no modified-date filter
3. Diff the full note list against state rows per project — creates any missing
   associations
4. Can be added as a separate path in the existing Lambda triggered by a manual
   invocation payload (e.g., `{"full_scan": true}`) rather than always running

---

## [open] Systematic employee data quality — triage before building more Lambdas

**Captured:** 2026-05-29

**Revisit when:** the next data quality issue surfaces and the team asks "should
we build a janitor Lambda for this?"

**Context:**
Late in the project-note-sync session, the user noted there's a lot of orphaned
records and misplaced information with employees (e.g., HubSpot records
incorrectly associated, data in the wrong place). The question was whether the
reconciliation-Lambda pattern we built is "common practice or overkill."

**Framework to apply before building each new fixer:**
1. **Classify the failure:** Is this a prevention problem (bad input), a
   detection problem (we don't know when it's wrong), or a correction problem
   (we know it's wrong, now fix it)?
2. **Is the root cause fixable?** A janitor Lambda that runs forever is a
   maintenance tax. If the upstream source of truth can be fixed (e.g., a form
   validation, a HubSpot workflow property rule), do that first.
3. **Volume and frequency:** ~11 records/30 days (project-note-sync scale) is
   worth a background Lambda. Thousands of records/day suggests a systemic
   input problem that needs a source fix, not a fixer.
4. **Idempotency cost:** Build state tables only when the scan is expensive
   (many API calls) and most records don't need work. For cheap scans
   (pure DB), skip the state table and always re-diff.

**Candidate issues to triage (collect before the next session on this topic):**
- Orphaned HubSpot company/contact associations (no project, no owner)
- Notes associated to wrong object type
- Any other "misplaced information" cases the user has observed

---

## [open] Harden GHL API key storage

**Captured:** 2026-05-29

**Revisit when:** team grows beyond one engineer with DB access, OR a security
review flags plaintext storage as unacceptable, OR `pg_dump` exposure becomes a
real concern (e.g., starting to share backups with anyone).

**Why it matters:**
Each GHL location has an API key stored as plaintext text in the
`client_lead_master.hl_sub_account_api_key` column. The keys were already
plaintext in the upstream Google Sheet and in Supabase, so adding them to RDS
didn't worsen exposure. But today's threat model relies on a single point of
access control: "only one person has DB credentials." That assumption gets
brittle the moment a second engineer is onboarded.

**Decision history:** Earlier in the session we evaluated Secrets Manager
(Option B), a separate role-restricted table in Postgres (Option D), and
plaintext-in-existing-table (Option A). Picked A because:
- The IAM boundary explicitly denies `secretsmanager:PutSecretValue` for
  Lambda roles, which is the right design — making it loose to auto-sync
  would loosen security for ALL Lambdas.
- The keys are already broadly internally visible (sheet is shared).
- Setting up Postgres role separation requires plumbing (multiple DB users,
  multiple Secrets Manager entries, per-Lambda connection logic) that doesn't
  exist anywhere else in this repo. Worth doing when there's actual benefit.

**What to build when revisiting:**
Recommended target is **Option B2** — Secrets Manager + manual refresh script:

1. Create AWS Secrets Manager secret `gymlaunch/ghl/location_api_keys` —
   single JSON blob keyed by `hl_sub_account_location_id`. Same shape as
   `gymlaunch/stripe/api_keys`.
2. Write `scripts/refresh_ghl_keys.py` — reads the `00 - Database` sheet,
   builds the `{location_id: api_key}` dict, calls `PutSecretValue`. Invoked
   from operator's machine (which has wider IAM than Lambda boundary
   allows). Run when keys rotate or new clients are added.
3. Update any GHL-calling Lambdas (FB lead forwarder, etc.) to fetch the
   secret on cold start, cache the parsed dict in module scope, look up
   keys by location_id.
4. Migration to drop `hl_sub_account_api_key` from `client_lead_master`
   (only after consumers are switched over).
5. Re-run sheet sync with the column gone — Lambda's `RDS_COLUMNS` list and
   `REQUIRED_SHEET_HEADERS` set both lose the entry; sheet itself keeps the
   column (it's used by n8n upstream).

**Alternative considered:** Option D-full (separate `client_api_keys` table
with role-restricted access). Same security properties as B2 but more
operational complexity (managing additional Postgres roles, per-Lambda
connection switching). B2 wins on simplicity.

---

## [open] SubscriptionFlow integration — hardening + spin-out

**Captured:** 2026-06-23

**Revisit when:** a second SF use case appears (anything beyond the single GO-product
subscribe endpoint), or SF accounts go multi-tenant, or the GO product's real
charge price is finalized.

**Why it matters:**
This started as a single Lambda (`gymlaunch-sf-create-custom-weekly-sub-for-go-product`)
plus a one-row OAuth token table. The user said it "will eventually spin out into
more." The current shape is deliberately minimal; several things were parked:

**Parked items:**
1. **Placeholder charge price.** When the incoming request omits `price`, the
   line-item charge defaults to **0.00** (agreed: "default to 0, we can fix it
   after the fact"). The invoice is left **due** (pay_invoice is off — see below),
   so a missing price produces a $0 invoice sitting in due/unpaid state. Revisit
   once the real GO-product price is known — change `DEFAULT_PRICE` in
   `src/subscriptionflow/create_sub/handler.py`, or make the caller always send `price`.
2. **Weekly cadence comes from the SF plan, not the code.** Clarified 2026-06-23:
   the goal is "a weekly subscription that runs for 1 year then ends." `type: "Termed"`
   + `termed_initial_period: 1` + `..._type: "year"` correctly encodes the 1-year
   fixed term that ends (Termed = ends; Evergreen = renews forever). But `POST
   /subscriptions` has **no billing-frequency field** — the weekly cadence is
   defined by the SF plan/price config. **Action: confirm the default plan
   `f359c92d-c0d7-4594-961a-f46158cb459f` is configured as a WEEKLY plan in the
   SF dashboard.** If it isn't, the cadence will be wrong regardless of this code.
3. **Single-tenant token table.** `subscriptionflow_oauth_token` is a singleton
   (`id = 1` CHECK). If SF ever holds multiple GymLaunch accounts, drop the
   singleton constraint and add an account-key column. See migration
   `013_subscriptionflow_schema.sql`.
4. **No proactive concurrency lock on rotation.** Rotation is in-band (proactive
   on expiry + reactive on 401). Within one invocation this is race-free, but two
   simultaneous cold invocations could both rotate. SF issues a fresh token each
   time and we UPSERT the latest, so the worst case is a redundant token fetch —
   acceptable for current low volume. Add a DB advisory lock if call volume rises.
5. **Vendor docs.** SF OpenAPI spec lives at
   `docs/vendor/subscriptionflow/openapi.json` for future endpoint work
   (cancel/suspend/resume, invoices, etc.).
6. **Named-plan registry (deferred 2026-06-23 — "ship simple now").** Today the
   caller relies on hardcoded ID defaults (or passes raw `product_id`/`plan_id`/
   `plan_price_id`), and the subscription shape (Termed/1yr/`pay_invoice:false`)
   is hardcoded. **Revisit when a SECOND product/plan appears.** Replace the raw
   IDs with a `plan` name the caller sends (e.g. `"plan": "weekly_go_custom"`)
   that maps to a catalog entry carrying the **full subscription shape**, so new
   plans (monthly, evergreen, auto-charging) need no code change:
       "weekly_go_custom": {
         "product_id": "fe483af0-...", "plan_id": "f359c92d-...",
         "plan_price_id": "f359c92d-...", "type": "Termed",
         "termed_initial_period": 1, "termed_initial_period_type": "year",
         "pay_invoice": false
       }
   Per-customer values (`id`, `email`, `price`, `start_date`) stay in the payload.
   Keep backward-compat: `plan` optional, explicit IDs override, no plan → default
   plan, unknown plan → 400. **Open decisions (unanswered):** catalog home
   (env-var JSON like `TWILIO_NUMBER_CHANNELS` vs in-code dict vs DB table) and
   whether entries carry full shape (recommended) or IDs only.
7. **Price input hardening (deferred 2026-06-24).** Confirmed end-to-end working
   under good input, but `resolve_price` only does `float(raw)` — so junk like
   `499.999` passes straight through to the SF charge. **Revisit before the
   endpoint takes arbitrary user/typed input.** Decisions to make:
   - **>2 decimal places** (`499.999`): round to 2dp for currency, or reject 400?
   - **Formatted strings** (`"$49"`, `"1,234.00"`, `"49 USD"`): currently 400
     `price must be numeric`. Add a sanitizer (strip `$`, `,`, whitespace) or keep
     strict? (Depends whether the webhook source can send formatted values.)
   - **Negative / zero / absurd values**: reject `< 0`? cap an upper bound? (Note
     `0` is the current default-when-omitted, so it must stay allowed.)
   Lower urgency because `pay_invoice` is off — a bad price creates a wrong-amount
   DUE invoice, not a live charge — but it's still bad data. Lives in
   `resolve_price()` in `src/subscriptionflow/create_sub/handler.py`.

**Setup still required before first use (one-time):**
- Create Secrets Manager secret `gymlaunch/subscriptionflow/api` with JSON
  `{"client_id","client_secret","endpoint_api_key"}`.
- Run migration `013_subscriptionflow_schema.sql`.
- `bash scripts/deploy.sh`, then enable a Function URL on the function in the
  console (deploy IAM user can't create Function URLs — same as add-slack-channel).

---

## [open] SubscriptionFlow payment-data sync → DB (billing fields + churn signal + alerting)

**Captured:** 2026-06-24 (direction firmed up same day)

**Revisit when:** the weekend Zapier test has shown what SF actually emits on a
failed transaction, or sooner if we decide to build the daily sync first.

**DECISION (2026-06-24): build the DB sync, don't do a direct SF→HubSpot push.**
The immediate ask is small — push invoice `amount_due` / `amount_paid` to HubSpot
(those don't sync natively). For *that alone* a DB would be overkill (it's a straight
field copy, no derivation). What flips it to "build the DB" is that payment behavior
is a **churn signal** for the "brain" ([[project_ai_brain]]): non-payment / slipping
payments / failed-then-recovered charges predict churn. That signal is **temporal**
— it lives in the history, and (a) HubSpot properties are last-value-wins (can't hold
history) and (b) **transactions don't sync to HubSpot at all**, so the DB is the only
home for the sharpest signal. Clinching argument: **history can't be backfilled** —
every day without the sync is payment history lost forever, so start banking it now.

**Goal:** reliable **failed-payment alerting** + a **payment-behavior data layer**
that serves billing-field display now and churn scoring later.

**Current interim step (user, this weekend):** SF dashboard webhook on failed
transactions → Zapier, just to observe what real failures look like over the
weekend. No code from us yet. Blocker on testing: can't reliably fabricate a
decline — needs a gateway test-decline card on a customer with `pay_invoice:true`
(only works in an SF sandbox/test gateway); on a live gateway you have to wait for
organic failures.

**Recommended architecture (both, not either/or):**
1. **Webhook (fast, lossy)** — real-time ping for the alert. Eventually replace
   Zapier with our own receiver Lambda (Function-URL or via the existing HttpApi)
   that validates + writes to a table and fires the alert (Slack/SES/etc.).
2. **Daily sync (reliable, complete)** — pull subs / transactions / invoices from
   SF into RDS tables on a daily cron (like the other `gymlaunch-*-sync` Lambdas).
   This is the reconciliation backbone: catches anything the webhook misses, is the
   source of truth, and makes alert logic testable (just query `status = failed`).
   Feeds the broader unified-activity / "AI brain" data layer.

**Consumers of the sync (build decoupled — don't block billing on the brain):**
1. **Billing fields → HubSpot (now, simple):** daily push of invoice `amount_due` /
   `amount_paid` (and later the rest of the billing list) read off the DB. Open
   sub-question: do those land 1:1 on the synced HubSpot **invoice objects** (easy,
   sidesteps identity mapping) or rolled up to **company/contact** (needs aggregation
   + SF-customer→HubSpot mapping)? User's wording suggests 1:1 invoice enrichment.
2. **Churn signal (later, analytical):** payment-behavior features for [[project_ai_brain]].
3. **Failed-payment alerting:** the reconciliation backbone behind the webhook.

**Scope discipline:** sync **payment objects only** (invoices + transactions, plus
subscriptions for status/frequency). Do NOT model all of SF speculatively — that's the
junk-drawer risk. Raw payment objects in, derived fields out.

**Key design decision (the important one):** store **latest-state per invoice**
(upsert — simple, current snapshot only) vs **capture state changes over time**
(due→failed→paid transitions — the churn-rich part). Churn needs the transitions, so
lean toward capturing status changes, not just overwriting the row.

**Schema: DRAFTED in migration `014_subscriptionflow_data_schema.sql`** (2026-06-30) —
5 tables `sf_customer`/`sf_subscription`/`sf_invoice`/`sf_transaction`/`sf_product`,
TEXT PKs, flat columns + `raw jsonb` per table, denormalized `primary_subscription_id`/
`primary_invoice_id`, no hard FKs, gls_writer grants. NOT yet applied.

**RESOLVED from live API responses (2026-06-30):**
- **Identity SOLVED:** `hubspot_id` is a real, populated field on customer/invoice/
  subscription; subscription also has `additional_data.hubspot_deal_id`. For HubSpot-sourced
  customers (`data_source='HubSpot'`) the SF id == hubspot_id. Deterministic join — no email
  matching. (Nullable for SF-created customers until they sync back.)
- **Cross-links are embedded** in detail responses: `invoice.items[].subscription_id`,
  `invoice.transactions[]`, `transaction.invoices[].invoice_id`, `subscription.items[].plan_price`.
  So real FK columns are populatable (not customer-key-only as first feared).
- **Billing frequency** = derive from embedded `plan_price.billing_period_months_weeks`
  (13 weeks = quarterly). Denormalized onto `sf_subscription.billing_frequency`. No plan table.
- **`next_bill_date`** is a real field → billing's "Next Payment Date." `payment_status`
  on the subscription = the "Invoice Status" rollup → billing's "Billing Status."
- `accounting_resource_id` = Sage Intacct id; `payment_type_id` = card mask; SF has native
  `primary_churn_score_value`/`_grade` fields (null now — maybe enable on their side).

**Sync Lambda: BUILT (2026-06-30, not deployed)** — `gymlaunch-subscriptionflow-daily-sync`
(`src/subscriptionflow/sync/handler.py`). Daily `cron(30 6 * * ? *)`, wired in
template.yaml + deploy.sh. `DEBUG=1` = dry run (one bounded page/object, no writes, returns
sample + pagination meta + request trace).

**Dry-run findings (2026-07-01) that reshaped it:**
- Endpoint: use **`GET /<obj>/with-relations`** (plain list works too but is thinner;
  `POST /<obj>/filter` returns empty with no condition). Pagination is **Laravel page-based**
  (`?page` / `meta.last_page` / `links.next`), NOT `filter[$offset]` (which SF ignores).
- **with-relations does NOT embed items[]/invoices[]** — only flat fields + customer_id. So
  the detail-fetch was dropped (it was an N+1 that timed out). Nested-derived columns
  (`primary_subscription_id`, `primary_invoice_id`, `plan_id`, `billing_frequency`) are left
  NULL for now — all billing + churn fields are flat. `raw` jsonb keeps everything.
- **Volume: ~116k customers (582 pages), 13k transactions, 10k invoices, 1.4k subs, 30
  products** @ ~2s/API call → a single run can't backfill. So the sync is now **resumable**:
  per-page upsert, `sf_sync_state` cursor (**migration 015**), timeout guard (stops <60s before
  the deadline, resumes next invocation), then incremental-by-watermark once backfilled.
- Mappers validated on live data (billing_frequency→Quarterly, onetime→PIF; failure signal
  visible as invoice status `Due` + note `"Payment failed[N]"`).

**STILL OPEN:**
- **Go live:** apply migrations `014` + `015`, deploy. Empty tables backfill automatically —
  the daily 06:30 cron will chip away at the customer backfill over several days (resumable),
  or invoke manually a few times to finish it faster. Watch each run's `stats` (`mode`,
  `pages`, `stopped_early`, `backfill_done`).
- **VERIFY incremental filter on the first post-backfill run.** The `filter[updated_at][$gte]`
  param is assumed-supported but unconfirmed. If a post-backfill run shows a huge `pages`
  count (re-pulling everything), SF isn't honoring it → switch to sort-desc + stop-at-watermark
  or a page cursor. Per-page upsert means even the bad case is correct, just slow.
- **~116k customers is a lot** — mostly HubSpot-synced contacts with no billing. Decide whether
  to sync all (resumable handles it) or scope to customers with billing activity. Not blocking.
- **Deferred nested fields** (`primary_subscription_id`/`primary_invoice_id`/`plan_id`/
  `billing_frequency`) — enrich later via a targeted pass (subs are only ~1.4k) or from `raw`.
- **Failed transaction status value + `decline_reason`** — success = `status="Paid"`; failure
  value unconfirmed. Invoice-level signal (`status=Due` + note `"Payment failed[N]"`) may be
  the better churn trigger anyway. Grab from the weekend webhook.
- **Customer outstanding balance** — not on the customer payload; compute from invoices
  (`SUM(closing_balance)`), handled by the downstream HubSpot-push consumer.
- **NEXT CONSUMER — the RDS→HubSpot push is still NOT built.** This sync only fills the DB.
  Pushing invoice amount_due/paid onto HubSpot (via invoice `hubspot_id`) is the billing-dept ask.
- Sync endpoints: `GET /subscriptions`, plus the transaction/invoice list endpoints
  (need to confirm exact paths/filters in `docs/vendor/subscriptionflow/openapi.json`).
- Full-load-then-incremental vs full daily pull; cadence; lookback window.
- Alert channel + dedupe (don't re-alert the same failed txn from both webhook and
  sync).
- RDS is `db.t4g.micro` (1 GB) — practical ~50–80 connection ceiling; this sync adds
  ~1 connection/run (negligible). See RDS notes (CPU idle, storage trivial).

---
