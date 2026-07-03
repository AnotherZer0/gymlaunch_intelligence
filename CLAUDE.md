# GymLaunch Intelligence — Claude Instructions

## Before writing any Lambda function

Always ask the user what to name the Lambda function before writing any code.
Do not invent or reuse a name without explicit confirmation.

**The name MUST start with `gymlaunch-` and use hyphens — never underscores.** The
`gymlaunch-deploy` IAM policy scopes Lambda actions to the ARN pattern `gymlaunch-*`
(literal hyphen), so an underscore name like `gymlaunch_foo` deploys with `AccessDenied`
on `lambda:GetFunction` and CloudFormation rolls the whole stack back. If the user
proposes underscores or a different prefix, flag it and convert to
`gymlaunch-...-with-hyphens` before writing any code. (First hit: the SF billing push,
named `gymlaunch_sync_...`, failed its deploy this way — renamed to
`gymlaunch-sync-sf-billing-info-to-hubspot`.)

## Debug / dry-run mode

Every Lambda or job we build that performs writes or outward side-effects (external
API calls, DB writes, creating records, sending messages, moving money) MUST ship with
a **`DEBUG` / dry-run mode**, gated by an env var (default `"0"`, declared in
`infra/template.yaml` so it's toggleable in the console):

- When on, it exercises the real logic — auth, fetches, mapping — but performs **no
  writes/side-effects**, and **returns** what it *would* do (the body it would POST, the
  rows it would upsert, the records it would touch) in the invocation/HTTP response.
- Put the dry-run output in the **response, not only `print()`** — the `gymlaunch-deploy`
  user cannot read CloudWatch logs, so a dry run whose output only lands in logs is
  useless. The response is where we read it.
- Keeping the flag in the template resets it to off on every deploy, so a stray dry-run
  flag can't survive to production. Pair with a `FULL_SYNC`-style flag for backfills.

This is how we catch problems before touching live data (it caught the SF subscribe
Lambda's body shape and the daily-sync's wrong list endpoint).

## Database migrations

Lambdas connect to RDS as **`gls_writer`** (the `DB_USER` env var). Migrations,
however, are often applied as a **personal/admin DB user**. In Postgres a table is
owned by whoever created it, and a table created by the personal user grants
`gls_writer` **nothing** — the Lambda then fails at runtime with
`42501 permission denied for table <name>`.

Therefore, **every migration that creates a table or sequence MUST include an
explicit `GRANT` to `gls_writer`** in the same file:

- `GRANT SELECT, INSERT, UPDATE[, DELETE] ON <table> TO gls_writer;` — grant only
  the verbs the consuming code actually uses (least privilege).
- For any `SERIAL`/`BIGSERIAL`/identity column, also
  `GRANT USAGE, SELECT ON SEQUENCE <seq> TO gls_writer;`.

These grants are idempotent and harmless when the migration is instead applied as
`gls_writer` (it just grants to itself). First codified after migration `013`
(`subscriptionflow_oauth_token`) hit this. The older tables predate the personal
user, so they were created as `gls_writer` and don't need it — but all new ones do.

## File write location

Always write files directly to `/mnt/data/gymlaunch_intelligence/` (the main repo path),
not to any `.claude/worktrees/` path. The user interacts with files at the main path
and should not have to commit a worktree to see changes.

## Future-work backlog

There is a running todo list at `docs/future_work.md`. It captures intentionally
deferred work across sessions — design decisions we parked, hardening passes
that should happen "eventually," partial migrations with the rest queued up.

- **At the start of any session** that touches a deferred area (e.g. anything
  around GHL API keys, the n8n FB-lead workflow replacement, etc.), open this
  file first and surface the relevant entry to the user. They may have
  forgotten the context.
- **When deferring new work mid-session,** add a new entry to this file with
  the date captured, why we deferred, and what would trigger picking it back
  up. Don't bury it inline in `system_reference.md` — that doc is for current
  state, `future_work.md` is for pending decisions.
- **When picking up work and completing it,** flip the entry's status tag
  from `[open]` to `[done]` and leave it in the file as a record (do not
  delete). Add the completion date.

## System reference

The complete reference for current infrastructure, Lambda functions, secrets,
database schema, and source-file layout is `docs/system_reference.md`. Read it
when you need authoritative answers about how things are wired today.
