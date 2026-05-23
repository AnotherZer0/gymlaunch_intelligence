"""
HubSpot project-note sync Lambda (gymlaunch-project-note-sync)

Back-fills the company + contact associations onto notes that are attached to a
HubSpot Project, so downstream activity rollups (and the future AI brain) see the
note in context. HubSpot doesn't fire a useful webhook on note creation against
Projects, so this runs nightly instead.

Why notes-first (not projects-first):
  The /crm/v3/objects/0-970 list-instances endpoint is gated behind a scope that
  isn't available to Private Apps for portal 43776308 ("scope isn't available for
  public use" — non-fixable from our side, support ticket pending). However the v4
  associations endpoints (project→companies, project→contacts, note→projects,
  note→companies, note→contacts) all work normally. So we iterate notes instead.

Algorithm per run:
  1. Search notes modified in the last N hours (default 36h, configurable via
     event {"lookback_hours": N}).
  2. Batch-read note→projects (0-970). Drop notes not attached to a project.
  3. Batch-read each unique project's companies and contacts.
  4. Filter (note, project) pairs against hubspot_project_note_sync. Skip pairs
     whose snapshot matches and where both sides are already marked synced.
  5. For pairs that need work, batch-read each note's CURRENT note→company and
     note→contact associations.
  6. Compute desired set per note = UNION of every linked project's companies +
     contacts. Diff against current. Batch-create the missing associations.
  7. Upsert state rows with per-side success — partial failures NULL the failing
     side's _synced_at so the unsynced-pair index resurfaces the pair next run.

One known gap (accepted by design):
  HubSpot does NOT bump a note's hs_lastmodifieddate when associations change. So
  an old note that gets newly attached to a project later won't surface in the
  search and won't be processed. Mitigation, if it bites: cache project IDs we've
  seen and periodically re-poll their note lists. Not built yet.
"""

import os
import ssl
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import pg8000
import requests


# --- Config ---

REQUEST_TIMEOUT_SECONDS = 30

HUBSPOT_BASE_URL        = "https://api.hubapi.com"
PROJECT_OBJECT_TYPE     = "0-970"
NOTE_SEARCH_PAGE_SIZE   = 100         # HubSpot search hard max
ASSOC_BATCH_SIZE        = 100         # HubSpot v4 batch endpoints hard max
DEFAULT_LOOKBACK_HOURS  = 96         # paired with the 72h schedule — 24h overlap
HUBSPOT_SEARCH_HARD_LIMIT = 10_000    # HubSpot caps search results — log if we hit it
ADVISORY_LOCK_KEY       = "project_note_sync"

# Default HUBSPOT_DEFINED association type IDs for notes → standard objects.
# Confirmed via GET /crm/v4/associations/notes/{type}/labels. If a future HubSpot
# rename changes these and we start getting 404s on /batch/create, re-query the
# labels endpoint and update.
ASSOC_DEFAULT_TYPE_IDS = {
    "companies": 190,
    "contacts":  202,
}


# --- DB ---

def get_db_connection():
    ctx = ssl.create_default_context()
    return pg8000.connect(
        host=os.environ["DB_HOST"],
        port=5432,
        database=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        ssl_context=ctx,
    )


# --- HubSpot HTTP ---

def hubspot_headers():
    return {
        "Authorization": f"Bearer {os.environ['HUBSPOT_TOKEN']}",
        "Content-Type": "application/json",
    }


def hubspot_request(method: str, path: str, json_body: dict | None = None,
                    params: dict | None = None) -> requests.Response:
    """Single HTTP call with one rate-limit-aware retry."""
    url = f"{HUBSPOT_BASE_URL}{path}"
    for attempt in range(2):
        resp = requests.request(
            method,
            url,
            headers=hubspot_headers(),
            json=json_body,
            params=params,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        if resp.status_code == 429 and attempt == 0:
            wait = int(resp.headers.get("Retry-After", 10))
            print(f"  Rate limited by HubSpot, waiting {wait}s and retrying")
            time.sleep(wait)
            continue
        return resp
    return resp


# --- Note search ---

def search_recent_note_ids(modified_since_ms: int):
    """Yield batches of note IDs modified at or after the given epoch-ms timestamp.

    Sorted ascending by hs_lastmodifieddate so paging is stable even if new notes
    arrive mid-run. Logs a warning if we hit HubSpot's 10k search hard limit (in
    which case some notes are missed and the next run will catch them since their
    modified date is still in the lookback window).
    """
    after = None
    seen_total = 0
    reported_total = None
    page_num = 0
    while True:
        page_num += 1
        body = {
            "filterGroups": [{
                "filters": [{
                    "propertyName": "hs_lastmodifieddate",
                    "operator":     "GTE",
                    "value":        modified_since_ms,
                }]
            }],
            "sorts":      [{"propertyName": "hs_lastmodifieddate", "direction": "ASCENDING"}],
            "properties": ["hs_lastmodifieddate"],
            "limit":      NOTE_SEARCH_PAGE_SIZE,
        }
        if after:
            body["after"] = after
        print(f"  [notes search page {page_num}] modified_since_ms={modified_since_ms} "
              f"after={after!r}")
        resp = hubspot_request("POST", "/crm/v3/objects/notes/search", json_body=body)
        if resp.status_code != 200:
            raise RuntimeError(
                f"Notes search failed: {resp.status_code} {resp.text[:300]}"
            )
        data = resp.json()
        results = data.get("results", [])
        if reported_total is None:
            reported_total = data.get("total")
            if reported_total is not None and reported_total > HUBSPOT_SEARCH_HARD_LIMIT:
                print(f"  WARNING: search total={reported_total} exceeds HubSpot's "
                      f"{HUBSPOT_SEARCH_HARD_LIMIT} hard limit — older modified notes "
                      f"will not be returned this run. Consider tightening lookback or "
                      f"re-invoking with a shorter window.")
        seen_total += len(results)
        print(f"  [notes search page {page_num}] {len(results)} result(s) "
              f"(running total {seen_total}/{reported_total})")
        if results:
            yield [str(r["id"]) for r in results]
        after = data.get("paging", {}).get("next", {}).get("after")
        if not after:
            print(f"  notes search complete after {page_num} page(s), {seen_total} total")
            return


# --- Association reads ---

def batch_read_associations(from_ids: list[str], from_type: str,
                            to_type: str) -> dict[str, set[str]]:
    """Returns {from_id: set(to_ids)} via /crm/v4/associations/{from}/{to}/batch/read.

    HubSpot returns objects with no associations in the `errors` array under
    subCategory crm.associations.NO_ASSOCIATIONS_FOUND — that's expected, not a
    failure. Real errors are surfaced as warnings.
    """
    out: dict[str, set[str]] = {fid: set() for fid in from_ids}
    if not from_ids:
        return out
    for chunk_start in range(0, len(from_ids), ASSOC_BATCH_SIZE):
        chunk = from_ids[chunk_start : chunk_start + ASSOC_BATCH_SIZE]
        resp = hubspot_request(
            "POST",
            f"/crm/v4/associations/{from_type}/{to_type}/batch/read",
            json_body={"inputs": [{"id": fid} for fid in chunk]},
        )
        if resp.status_code not in (200, 207):
            raise RuntimeError(
                f"Batch read {from_type}→{to_type} failed: "
                f"{resp.status_code} {resp.text[:300]}"
            )
        data = resp.json()
        for result in data.get("results", []):
            from_id = str(result.get("from", {}).get("id"))
            tos = result.get("to", []) or []
            out.setdefault(from_id, set()).update(
                str(t["toObjectId"]) for t in tos if "toObjectId" in t
            )
        for err in data.get("errors", []) or []:
            sub = err.get("subCategory", "")
            if "NO_ASSOCIATIONS_FOUND" in sub:
                continue  # object has zero associations on this side — fine
            print(f"  WARNING: {from_type}→{to_type} batch read error: {err}")
    return out


# --- Association creates ---

def batch_create_associations(to_object_type: str,
                              pairs: list[tuple[str, str]]) -> set[tuple[str, str]]:
    """Create default associations notes→to_object_type for each (note_id, to_id).
    Returns the set of pairs that FAILED."""
    if not pairs:
        return set()
    type_id = ASSOC_DEFAULT_TYPE_IDS[to_object_type]
    failures: set[tuple[str, str]] = set()
    for chunk_start in range(0, len(pairs), ASSOC_BATCH_SIZE):
        chunk = pairs[chunk_start : chunk_start + ASSOC_BATCH_SIZE]
        inputs = [{
            "from": {"id": nid},
            "to":   {"id": tid},
            "types": [{
                "associationCategory": "HUBSPOT_DEFINED",
                "associationTypeId":   type_id,
            }],
        } for nid, tid in chunk]
        resp = hubspot_request(
            "POST",
            f"/crm/v4/associations/notes/{to_object_type}/batch/create",
            json_body={"inputs": inputs},
        )
        if resp.status_code in (200, 201):
            continue
        if resp.status_code == 207:
            data = resp.json()
            for err in data.get("errors", []) or []:
                ctx = err.get("context") or {}
                from_ids = ctx.get("fromObjectIds") or []
                to_ids   = ctx.get("toObjectIds") or []
                if from_ids and to_ids:
                    for fid in from_ids:
                        for tid in to_ids:
                            failures.add((str(fid), str(tid)))
                else:
                    failures.update(chunk)
                    break
            continue
        print(f"  Batch create notes→{to_object_type} failed: "
              f"{resp.status_code} {resp.text[:300]}")
        failures.update(chunk)
    return failures


# --- State table ---

def fetch_state_rows(cur, pairs: list[tuple[str, str]]) -> dict[tuple[str, str], dict]:
    if not pairs:
        return {}
    note_ids    = [p[0] for p in pairs]
    project_ids = [p[1] for p in pairs]
    cur.execute(
        """
        SELECT note_id, project_id, project_company_ids, project_contact_ids,
               companies_synced_at, contacts_synced_at
        FROM hubspot_project_note_sync
        WHERE (note_id, project_id) IN (
            SELECT UNNEST(%s::TEXT[]), UNNEST(%s::TEXT[])
        )
        """,
        (note_ids, project_ids),
    )
    cols = [d[0] for d in cur.description]
    out = {}
    for row in cur.fetchall():
        d = dict(zip(cols, row))
        out[(d["note_id"], d["project_id"])] = d
    return out


def upsert_state_row(cur, note_id: str, project_id: str,
                     project_company_ids: list[str], project_contact_ids: list[str],
                     companies_synced: bool, contacts_synced: bool,
                     error: str | None) -> None:
    cur.execute(
        """
        INSERT INTO hubspot_project_note_sync
            (note_id, project_id, project_company_ids, project_contact_ids,
             companies_synced_at, contacts_synced_at,
             last_attempted_at, last_error, attempts, updated_at)
        VALUES
            (%s, %s, %s, %s,
             CASE WHEN %s THEN now() ELSE NULL END,
             CASE WHEN %s THEN now() ELSE NULL END,
             now(), %s, 1, now())
        ON CONFLICT (note_id, project_id) DO UPDATE SET
            project_company_ids = EXCLUDED.project_company_ids,
            project_contact_ids = EXCLUDED.project_contact_ids,
            companies_synced_at = CASE WHEN %s THEN now() ELSE NULL END,
            contacts_synced_at  = CASE WHEN %s THEN now() ELSE NULL END,
            last_attempted_at   = now(),
            last_error          = %s,
            attempts            = hubspot_project_note_sync.attempts + 1,
            updated_at          = now()
        """,
        (
            note_id, project_id, project_company_ids, project_contact_ids,
            companies_synced, contacts_synced, error,
            companies_synced, contacts_synced, error,
        ),
    )


# --- Diff logic ---

def pair_needs_work(state: dict | None,
                    project_company_ids: list[str],
                    project_contact_ids: list[str]) -> bool:
    if state is None:
        return True
    snapshot_drift = (
        set(state["project_company_ids"] or []) != set(project_company_ids) or
        set(state["project_contact_ids"] or []) != set(project_contact_ids)
    )
    if snapshot_drift:
        return True
    return state["companies_synced_at"] is None or state["contacts_synced_at"] is None


# --- Main sync ---

def sync(conn, lookback_hours: int) -> dict:
    cur = conn.cursor()

    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    modified_since_ms = int(cutoff.timestamp() * 1000)
    print(f"Looking up notes modified at or after {cutoff.isoformat()} "
          f"(lookback {lookback_hours}h, ms={modified_since_ms})")

    notes_seen          = 0
    notes_on_project    = 0
    pairs_skipped       = 0
    pairs_worked        = 0
    company_assoc_made  = 0
    contact_assoc_made  = 0
    errors              = 0

    for note_id_batch in search_recent_note_ids(modified_since_ms):
        notes_seen += len(note_id_batch)

        # Which of these notes are attached to a project?
        note_to_projects = batch_read_associations(note_id_batch, "notes", PROJECT_OBJECT_TYPE)
        project_notes = {nid: pids for nid, pids in note_to_projects.items() if pids}
        notes_on_project += len(project_notes)
        print(f"    {len(project_notes)}/{len(note_id_batch)} note(s) attached to a project")

        if not project_notes:
            continue

        # Read each unique project's companies + contacts once per batch.
        all_project_ids = sorted({pid for pids in project_notes.values() for pid in pids})
        project_companies = batch_read_associations(all_project_ids, PROJECT_OBJECT_TYPE, "companies")
        project_contacts  = batch_read_associations(all_project_ids, PROJECT_OBJECT_TYPE, "contacts")
        print(f"    fetched parties for {len(all_project_ids)} unique project(s)")

        # Build the candidate pair list: every (note, project) the search surfaced.
        candidate_pairs: list[tuple[str, str, list[str], list[str]]] = []
        for nid, pids in project_notes.items():
            for pid in pids:
                cps = sorted(project_companies.get(pid, set()))
                cts = sorted(project_contacts.get(pid, set()))
                candidate_pairs.append((nid, pid, cps, cts))

        # Filter against state table — most steady-state pairs skip here.
        pair_keys = [(nid, pid) for nid, pid, _, _ in candidate_pairs]
        state_by_pair = fetch_state_rows(cur, pair_keys)
        work_pairs: list[tuple[str, str, list[str], list[str]]] = []
        for nid, pid, cps, cts in candidate_pairs:
            if pair_needs_work(state_by_pair.get((nid, pid)), cps, cts):
                work_pairs.append((nid, pid, cps, cts))
            else:
                pairs_skipped += 1

        if not work_pairs:
            print(f"    all {len(candidate_pairs)} pair(s) already synced, no work")
            continue
        print(f"    {len(work_pairs)}/{len(candidate_pairs)} pair(s) need work "
              f"({len(candidate_pairs) - len(work_pairs)} skipped by state table)")

        # Read each note's CURRENT company + contact associations for the diff.
        note_ids_to_check = sorted({nid for nid, _, _, _ in work_pairs})
        current_companies = batch_read_associations(note_ids_to_check, "notes", "companies")
        current_contacts  = batch_read_associations(note_ids_to_check, "notes", "contacts")

        # Desired = UNION across every project the note is on in this batch.
        desired_companies_by_note: dict[str, set[str]] = defaultdict(set)
        desired_contacts_by_note:  dict[str, set[str]] = defaultdict(set)
        for nid, _, cps, cts in work_pairs:
            desired_companies_by_note[nid].update(cps)
            desired_contacts_by_note[nid].update(cts)

        company_creates: list[tuple[str, str]] = []
        contact_creates: list[tuple[str, str]] = []
        for nid in note_ids_to_check:
            for cid in desired_companies_by_note[nid] - current_companies.get(nid, set()):
                company_creates.append((nid, cid))
            for ctid in desired_contacts_by_note[nid] - current_contacts.get(nid, set()):
                contact_creates.append((nid, ctid))

        print(f"    creating {len(company_creates)} co + {len(contact_creates)} ct "
              f"association(s)")
        company_failures = batch_create_associations("companies", company_creates)
        contact_failures = batch_create_associations("contacts", contact_creates)
        if company_failures:
            print(f"    {len(company_failures)} co association(s) FAILED: "
                  f"{sorted(company_failures)[:5]}{'...' if len(company_failures) > 5 else ''}")
        if contact_failures:
            print(f"    {len(contact_failures)} ct association(s) FAILED: "
                  f"{sorted(contact_failures)[:5]}{'...' if len(contact_failures) > 5 else ''}")

        company_assoc_made += len(company_creates) - len(company_failures)
        contact_assoc_made += len(contact_creates) - len(contact_failures)

        # Per-pair outcome: a side is "synced" iff every required ID on that side is
        # present on the note after this run (either already there, or just created
        # without failure).
        for nid, pid, cps, cts in work_pairs:
            pairs_worked += 1
            post_co = current_companies.get(nid, set()) | {
                c for n, c in company_creates if n == nid and (n, c) not in company_failures
            }
            post_ct = current_contacts.get(nid, set()) | {
                c for n, c in contact_creates if n == nid and (n, c) not in contact_failures
            }
            companies_ok = set(cps).issubset(post_co)
            contacts_ok  = set(cts).issubset(post_ct)

            err_parts = []
            if not companies_ok:
                err_parts.append(f"companies missing: {sorted(set(cps) - post_co)}")
            if not contacts_ok:
                err_parts.append(f"contacts missing: {sorted(set(cts) - post_ct)}")
            err = "; ".join(err_parts) if err_parts else None
            if err:
                errors += 1

            upsert_state_row(cur, nid, pid, cps, cts, companies_ok, contacts_ok, err)

        conn.commit()
        print(f"    committed: +{len(company_creates) - len(company_failures)} co / "
              f"+{len(contact_creates) - len(contact_failures)} ct associations, "
              f"{len(work_pairs)} state row(s) upserted")

    return {
        "lookback_hours":     lookback_hours,
        "notes_seen":         notes_seen,
        "notes_on_project":   notes_on_project,
        "pairs_skipped":      pairs_skipped,
        "pairs_worked":       pairs_worked,
        "company_assoc_made": company_assoc_made,
        "contact_assoc_made": contact_assoc_made,
        "errors":             errors,
    }


# --- Lambda entry point ---

def lambda_handler(event, context):
    lookback_hours = DEFAULT_LOOKBACK_HOURS
    if isinstance(event, dict) and event.get("lookback_hours") is not None:
        try:
            lookback_hours = int(event["lookback_hours"])
        except (TypeError, ValueError):
            print(f"Invalid lookback_hours in event ({event['lookback_hours']!r}), "
                  f"falling back to default {DEFAULT_LOOKBACK_HOURS}")

    print(f"Project-note sync starting (lookback_hours={lookback_hours})")

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (ADVISORY_LOCK_KEY,))
        if not cur.fetchone()[0]:
            print("Another project-note sync is running, exiting")
            return {"statusCode": 200, "body": "Skipped: sync already running"}

        summary = sync(conn, lookback_hours)
        print(f"Project-note sync complete: {summary}")
        return {"statusCode": 200, "body": summary}

    except Exception as e:
        print(f"ERROR during project-note sync: {e}")
        raise
    finally:
        conn.close()
