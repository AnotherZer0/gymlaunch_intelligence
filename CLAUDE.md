# GymLaunch Intelligence — Claude Instructions

## Before writing any Lambda function

Always ask the user what to name the Lambda function before writing any code.
Do not invent or reuse a name without explicit confirmation.

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
