# Future integrations roadmap

This document describes **planned** third-party surfaces for the agent platform. It does **not** commit schema or Lambdas until each integration is prioritized. The core database and build strategy live in [`agend_db_and_strategy_49bb15d9.plan.md`](../agend_db_and_strategy_49bb15d9.plan.md).

**Principles**

- **RDS + migrations** remain the source of truth for schema; add tables when an integration is implemented, not before.
- Extend existing patterns: **`client_external_id`**, **`content_chunk.source_type`**, raw vs derived data, **proposal + approval** for CRM writes (not blind LLM writes).
- **Secrets:** AWS Secrets Manager or SSM Parameter Store (SecureString); avoid long-lived tokens in git or plaintext env files.
- **PII / payments:** Do **not** store raw payment instrument data (full card/bank details, Stripe payment method payloads, etc.). Storing **name, email**, and **accounting IDs** (e.g. Intacct customer/entity id) for join keys and ops is in scope; justify anything richer under data-minimization and retention policy.

---

## Table of systems

| System | Category | Data role | Priority | Owner |
|--------|----------|-----------|----------|-------|
| HubSpot | CRM / sales | Identity, export (tasks), automation target | TBD | TBD |
| Go High Level | CRM / marketing | Identity, automation (optional) | TBD | TBD |
| Asana | Work management | Ingest/sync, field metadata | TBD | TBD |
| Zendesk | Support | Ingest (tickets/comments) | TBD | TBD |
| Google Sheets | Ops / config | Channel maps, exports (v1 ops) | TBD | TBD |
| SubscriptionFlow | Billing | Primary subscription mirror; customer/subscription sync | TBD | TBD |
| Zoho Subscriptions | Billing | Legacy: few remaining clients; mirror after SF is stable | TBD | TBD |
| Stripe | Billing / payments | Customer/subscription/payment intent refs (no full PAN); future | TBD | TBD |
| Intacct | Finance / ERP | Entity/customer IDs, GL ties; investigate scope | TBD | TBD |
| Zoho Sign | Documents | Signature status, metadata | TBD | TBD |
| PandaDocs | Documents | Templates, status; optional text for RAG | TBD | TBD |

**Data role** values: `ingest` (pull into DB), `export` (push approved changes out), `identity only` (IDs on `client_account`), `automation` (orchestration, not primary fact store).

Update **Priority** and **Owner** when you schedule work.

---

## Per-system notes

### CRM / sales / marketing

**HubSpot** — System of record for CRM when integrated. The platform holds **proposals and staging**; execution (e.g. Breeze-style apply) runs after approval. External ID types to map: company, contact, deal, task (and others as needed).

**Go High Level** — Often overlaps CRM + automation + comms. **Decide explicitly:** if both HubSpot and GHL are used, which is **canonical CRM** for a given workflow, and how `client_external_id` rows avoid duplicate/conflicting truth (see [External ID naming](#external-id-naming-client_external_idsystem)).

### Work management

**Asana** — Tasks/projects as external work items. **Do not** hardcode per-project field lists in the repo. Use **project discovery** and **field introspection** (see [Asana: dynamic projects and custom fields](#asana-dynamic-projects-and-custom-fields)). Sync to HubSpot requires a **mapping layer** and **pre-flight validation** (see [HubSpot ↔ Asana](#hubspot--asana-dropdown-and-enum-safety)).

### Support

**Zendesk** — Tickets/comments as a future support corpus (similar in spirit to threaded Slack). Plan **client/org mapping** and **sensitivity** per brand when implemented. Schema choice at implementation time: dedicated `zendesk_*` tables vs a generic external-document pattern.

### Ops / config

**Google Sheets** — v1 operational channel maps and exports—not the primary fact store. Access via Sheets API (Lambda + service account) or CSV; store credentials in Secrets Manager.

### Billing / subscriptions

**Intended order:** (1) **SubscriptionFlow** to RDS + HubSpot “client” signals; (2) **Zoho Subscriptions** for the **small legacy** cohort; (3) **Stripe** when you need gateway-level IDs, payouts, or unified payment events—design so **one** logical “is paying client” rule can span SF + legacy Zoho + Stripe without triple-writing CRM truth.

**SubscriptionFlow** — Primary billing mirror for new work. Map customers/subscriptions via `client_external_id`; scheduled sync first; webhooks later. See [PII / payment data](#pii--payment-data).

**Zoho Subscriptions** — **Legacy:** only a few clients remain; treat as **read-only mirror** into RDS after SF patterns are proven. Same `zoho_subscriptions` external IDs; explicit **priority** if both SF and Zoho rows exist for one internal client (e.g. SF wins for “current” subscription).

**Stripe** — Plan for **customer id**, **subscription id**, invoice/charge ids as needed—**not** full payment method numbers or sensitive Stripe payloads in Postgres. Use Stripe’s APIs/dashboard for PCI scope; store references only.

**Intacct** — Investigate how **customer/entity** IDs align to HubSpot companies and SubscriptionFlow/Zoho customers; likely `client_external_id` with `system = intacct` and `id_type` for entity vs customer record. Scope: reporting joins and “official” accounting identity—not full GL replication unless you later add a dedicated reporting pipeline.

**Zoho Sign** — Signature **status** and **document metadata** more than full body text; clarify overlap with PandaDocs if both appear.

### Documents / contracts

**PandaDocs** — Document/template IDs and status. If **full text** is needed for RAG, plan a dedicated ingest path (API/export/PDF) with the same **sensitivity** and **indexing_allowed** patterns as transcripts.

**Overlap:** If Zoho Sign and PandaDocs both participate in “signed agreement” workflows, document **one canonical tool** per workflow or how conflicts are reconciled.

---

## External ID naming (`client_external_id.system`)

Use **lowercase snake_case** API-stable strings. Examples:

| `system` value | Use |
|----------------|-----|
| `hubspot` | HubSpot CRM object IDs |
| `gohighlevel` | Go High Level IDs (if used) |
| `asana` | Workspace/project/task GIDs |
| `zendesk` | Org/user/ticket IDs |
| `subscriptionflow` | SubscriptionFlow IDs |
| `zoho_subscriptions` | Zoho Subscriptions customer/subscription IDs |
| `stripe` | Stripe customer, subscription, or connected account IDs (reference only) |
| `intacct` | Sage Intacct customer, entity, or other stable record IDs |
| `zoho_sign` | Zoho Sign envelope/document IDs |
| `pandadocs` | PandaDocs document IDs |

**`id_type`** should name the object kind (e.g. `company`, `contact`, `deal`, `task`, `project`, `ticket`).

**CRM source of truth:** If HubSpot and GHL both exist, record in this doc (or a one-line ADR): which system owns **canonical** company/deal data for sync and reporting. Prefer **one** primary CRM ID path per `client_account` unless you have a clear multi-CRM policy.

**Billing / ERP:** When **SubscriptionFlow**, **Zoho Subscriptions**, **Stripe**, and **Intacct** all appear, document **precedence** for “who is a paying client” (e.g. SF active sub OR legacy Zoho OR Stripe subscription) and how **Intacct** ties to the same `client_account`.

---

## PII / payment data

- **Avoid in RDS:** Full payment credentials, complete Stripe PaymentMethod objects, card numbers, bank account numbers, or anything PCI scope can handle in the upstream system.
- **Generally OK for joins and ops:** **Name**, **email**, **external IDs** (SubscriptionFlow/Zoho/Stripe/Intacct **identifiers**), subscription status snapshots, amounts at line level if required for reporting (confirm with finance/legal).
- **Intacct ID** — Store as **`client_external_id`** (or equivalent) for reconciliation with finance; avoid duplicating entire Intacct datasets unless a product requirement emerges.

---

## Asana: dynamic projects and custom fields

**Problem:** Many projects with different custom fields; people add dropdown options in Asana without updating HubSpot or application code.

**Approach (when Asana sync is built):**

1. **Project discovery** — Avoid a static list of projects in code. Prefer: Asana API (workspace/portfolio/team membership), a **config table** of `project_gid` rows to sync (ops-editable), or rules such as “all projects in team X.” Adding a project should be **data/config**, not a code deploy.

2. **Field introspection (schema pull)** — On a schedule (and optionally via webhooks for project/field changes), call Asana APIs for **custom field definitions** per project/workspace: `gid`, name, type (`enum`, `text`, `number`, …), and for enums the **current option list** (name + gid). Persist snapshots in Postgres, e.g. conceptually:

   - `source = asana`
   - `container_gid` (project or workspace)
   - `field_gid`, `field_type`, `enum_options_json`, `synced_at`

   This table is **metadata**, not task bodies; refresh often.

3. **Task sync** — Resolve enum values **by option gid** where possible (stable); names can change. If a task references an option gid missing from the latest snapshot, treat as **schema drift**: refresh schema and retry, or **quarantine** with a structured error.

4. **HubSpot mapping** — Separate **mapping layer** (rows or config): Asana `field_gid` ↔ HubSpot property name; for enums, **Asana option gid ↔ HubSpot option id**. Mappings may be seeded from introspection but should be **governed** so ad-hoc new Asana options do not silently push bad data unless policy allows.

---

## HubSpot ↔ Asana: dropdown and enum safety

**Failure mode:** Asana allows new dropdown options anytime; HubSpot enumeration properties are a **closed set** unless options are added via API/UI.

**Direction:**

1. **Pre-flight validation** — Before HubSpot writes, resolve values against a **cached HubSpot property definition** (CRM properties API: allowed options for enumeration fields). If there is **no valid mapping** from the Asana value, **do not** POST—emit a structured failure (e.g. `ENUM_MAPPING_MISSING`, `HUBSPOT_OPTION_UNKNOWN`) with context: project, task gid, field gid, Asana option gid/name.

2. **Quarantine / review** — Failed rows enter a **review** state (DB queue, internal tool, or agreed workflow)—same spirit as **staging** in the main plan—not silent drops.

3. **Policy options (choose explicitly):** map unknowns to a designated **“Other”** HubSpot option plus raw text elsewhere; **auto-create** HubSpot options only if allowed; **block** sync for a project until mappings exist.

4. **Observability** — Metrics such as `sync_failures_total{reason=...}`, `enum_mapping_missing_total`; tie to [Alerts](#alerts-sync-failures-and-schema-drift).

---

## Alerts: sync failures and schema drift

**Goal:** e.g. a new Asana dropdown option breaks HubSpot sync—you find out **without** digging through raw logs only.

| Layer | Role |
|-------|------|
| Application | Structured logs and metrics from sync jobs (success/fail, `error_code`). |
| AWS | CloudWatch alarms on Lambda errors, throttles, DLQ depth if queues are used. |
| Notification | SNS → email, Slack incoming webhook, or PagerDuty—pick a **primary** channel. |

**Minimum viable:** JSON log lines with `{ "error_code", "asana_task_gid", "field_gid", ... }` plus an alarm on Lambda errors or a metric filter on `ENUM_MAPPING_MISSING`. A **Slack** channel (e.g. `#integrations-alerts`) is often enough for ops.

**Documentation:** Note **who** is notified and whether alerts are best-effort or paging.

---

## Schema touchpoints (when implementing)

Implement **only** when an integration is scheduled:

- **Zendesk:** Likely `zendesk_ticket` / `zendesk_comment` or a generic external thread model—decide at design time.
- **Asana ↔ HubSpot:** `integration_field_schema` (or equivalent), mapping tables, optional `sync_quarantine` / failure rows.
- **Billing/docs:** Mostly `client_external_id` plus narrow fact tables for snapshots if needed; SF first, then legacy Zoho, then Stripe/Intacct as scoped.

---

## Non-goals for v1 core platform

Until **Slack/Zoom ingest**, **retrieval contract**, and **RDS baseline** are stable:

- No production Asana/HubSpot **sync** Lambdas required for the first milestone.
- No empty “placeholder” tables for every vendor.
- No CRM **duplication** of full HubSpot state in Postgres—prefer IDs and small cached snapshots where needed.

Reprioritization is allowed; update this doc when scope changes.

---

## Summary

- One **roadmap doc** (this file) aligns product and engineering before code lands.
- **`client_external_id.system`** uses stable lowercase snake_case; clarify **one CRM source of truth** if multiple CRMs appear.
- **Asana** uses **API-driven projects** and **stored field snapshots**, not hardcoded schemas.
- **HubSpot writes** follow **validation → quarantine → alert** when enums do not line up.
- **Billing stack:** **SubscriptionFlow** first; **Zoho Subscriptions** for remaining legacy accounts; **Stripe** and **Intacct** as references—**no** raw payment blobs in RDS; **name/email/Intacct id** acceptable for joins when policy allows.
