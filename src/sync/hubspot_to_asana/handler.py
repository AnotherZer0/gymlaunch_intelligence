"""
HubSpot -> Asana coach sync Lambda   (gymlaunch-sync-hubspot-to-agency-board)

Runs once a day. Moves the COACH assignment in ONE direction only:

    HubSpot company.coach (a HubSpot user id)  ->  database  ->  Asana "Coach" field

It never writes back to HubSpot and never clears anything. The existing hourly
outbound sync (gymlaunch-sync-agency-board-to-hubspot) still owns the
"coach == 'Agency Pro' -> clear HubSpot coach" rule; this Lambda does not touch it.

Two stages, in order, in a single invocation:

  Stage 1  HubSpot -> DB
      For each linked company, read the `coach` property (a HubSpot user id),
      translate it to a name via COACH_BY_HUBSPOT_USER_ID, and store the name in
      asana_agency_board_task.hs_coach. Empty coach or unknown id -> left alone.

  Stage 2  DB -> Asana
      Write hs_coach onto the Asana "Coach" multi-select field, but ONLY when:
        - it differs from the live Asana value (hs_coach != coach), AND
        - the card isn't a protected non-HubSpot value (see PROTECTED_ASANA_COACHES).
      Comparing against `coach` (the hourly mirror of the live Asana value) means
      a quiet day does zero writes, and HubSpot always wins: a manual Asana coach
      edit is corrected on the next run.

Matching key: asana_agency_board_task.hubspot_company_id  <->  HubSpot company id.
"""

import os
import ssl
import time

import asana
import pg8000
import requests


# =============================================================================
# EDITABLE MAP — coach roster
# When a coach joins or leaves, edit THIS and nothing else, then redeploy.
#   left  = the HubSpot user id that the company `coach` field returns
#   right = the Asana "Coach" dropdown option name (must match Asana exactly)
# If HubSpot returns an id that isn't listed here, that company is skipped and
# logged so you know to add it.
# =============================================================================
COACH_BY_HUBSPOT_USER_ID = {
    "75556992": "Ryan",
    "65419567": "RE",
    "1080854659": "RE",      # RE / R Lewis has a second HubSpot user id; ~66 companies use it
    "75559001": "Matt M",
    "75559000": "Blake",
    "75558999": "Rod",
    "75558917": "LAUNCH - Mik",   # Mikealea
}

# --- Field config (rarely changes) ---
HUBSPOT_PROPERTY      = "coach"               # company property, type "HubSpot user" -> returns a user id
ASANA_COACH_FIELD_GID = "1206012401384430"    # Asana "Coach" custom field (multi_enum / multi-select)

# Asana Coach values that are NOT HubSpot coaches. If a card currently shows one
# of these, the sync never overrides it from HubSpot. Add names here to protect more.
# (LAUNCH - Mik is a real HubSpot coach now, so it is NOT protected — it's in the map.)
PROTECTED_ASANA_COACHES = {"Agency Pro", "Alliance"}

HUBSPOT_BASE_URL        = "https://api.hubapi.com"
HUBSPOT_BATCH_SIZE      = 100                 # HubSpot max per batch read
REQUEST_TIMEOUT_SECONDS = 30


# --- Connections ---

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


def get_asana_client():
    configuration = asana.Configuration()
    configuration.access_token = os.environ["ASANA_TOKEN"]
    return asana.ApiClient(configuration)


def hubspot_headers():
    return {
        "Authorization": f"Bearer {os.environ['HUBSPOT_TOKEN']}",
        "Content-Type": "application/json",
    }


# --- HubSpot read ---

def _handle_rate_limit(resp: requests.Response) -> None:
    if resp.status_code == 429:
        wait = int(resp.headers.get("Retry-After", 10))
        print(f"  Rate limited by HubSpot, waiting {wait}s")
        time.sleep(wait)


def fetch_hubspot_coach(company_ids: list) -> dict:
    """
    Batch-read the `coach` property for the given company ids.
    Returns {company_id: user_id_or_None}.
    """
    values = {}
    url = f"{HUBSPOT_BASE_URL}/crm/v3/objects/companies/batch/read"

    for i in range(0, len(company_ids), HUBSPOT_BATCH_SIZE):
        chunk = company_ids[i : i + HUBSPOT_BATCH_SIZE]
        payload = {"properties": [HUBSPOT_PROPERTY], "inputs": [{"id": c} for c in chunk]}
        resp = requests.post(url, json=payload, headers=hubspot_headers(),
                             timeout=REQUEST_TIMEOUT_SECONDS)
        _handle_rate_limit(resp)
        if resp.status_code == 429:
            resp = requests.post(url, json=payload, headers=hubspot_headers(),
                                 timeout=REQUEST_TIMEOUT_SECONDS)

        if resp.status_code not in (200, 207):
            print(f"  HubSpot batch read failed: {resp.status_code} {resp.text[:300]}")
            continue

        for result in resp.json().get("results", []):
            cid = result.get("id")
            values[cid] = (result.get("properties") or {}).get(HUBSPOT_PROPERTY) or None

    return values


# --- Asana Coach field options ---

def is_protected(coach_now) -> bool:
    """True if the card's current Asana coach is a protected (non-HubSpot) value."""
    if not coach_now:
        return False
    current = {c.strip().lower() for c in coach_now.split(",")}
    return bool(current & {p.lower() for p in PROTECTED_ASANA_COACHES})


def load_coach_options(api_client) -> dict:
    """Return {lowercased_option_name: option_gid} for the Asana Coach field."""
    cf_api = asana.CustomFieldsApi(api_client)
    field = cf_api.get_custom_field(
        ASANA_COACH_FIELD_GID,
        {"opt_fields": "resource_subtype,enum_options.gid,enum_options.name,enum_options.enabled"},
    )
    out = {}
    for opt in field.get("enum_options", []) or []:
        name = (opt.get("name") or "").strip().lower()
        if name:
            out[name] = opt.get("gid")
    print(f"Loaded {len(out)} Asana Coach options (field subtype: {field.get('resource_subtype')})")
    return out


# --- Stage 1: HubSpot -> DB ---

def stage1_hubspot_to_db(conn) -> None:
    cur = conn.cursor()
    cur.execute(
        "SELECT DISTINCT hubspot_company_id FROM asana_agency_board_task "
        "WHERE hubspot_company_id IS NOT NULL"
    )
    company_ids = [r[0] for r in cur.fetchall()]
    if not company_ids:
        print("Stage 1: no linked companies, nothing to read")
        return

    print(f"Stage 1: reading `{HUBSPOT_PROPERTY}` for {len(company_ids)} companies from HubSpot")
    raw = fetch_hubspot_coach(company_ids)

    updated = empty = unmapped = 0
    for cid, user_id in raw.items():
        if not user_id:
            empty += 1                      # empty HubSpot coach -> leave hs_coach alone (protects Agency Pro)
            continue
        name = COACH_BY_HUBSPOT_USER_ID.get(str(user_id))
        if not name:
            print(f"  Unmapped HubSpot user id {user_id} (company {cid}) "
                  f"— add it to COACH_BY_HUBSPOT_USER_ID")
            unmapped += 1
            continue
        # Only write when it actually changes (honors "update if there's a diff").
        cur.execute(
            """
            UPDATE asana_agency_board_task
            SET hs_coach = %s, updated_at = now()
            WHERE hubspot_company_id = %s
              AND hs_coach IS DISTINCT FROM %s
            """,
            (name, cid, name),
        )
        if cur.rowcount:
            updated += cur.rowcount

    conn.commit()
    print(f"Stage 1 done: {updated} row(s) updated, {empty} empty (skipped), {unmapped} unmapped (skipped)")


# --- Stage 2: DB -> Asana ---

def stage2_db_to_asana(conn, api_client) -> None:
    cur = conn.cursor()
    # Push only where HubSpot's coach differs from the live Asana value (`coach`).
    cur.execute(
        """
        SELECT task_gid, coach, hs_coach
        FROM asana_agency_board_task
        WHERE hubspot_company_id IS NOT NULL
          AND hs_coach IS NOT NULL
          AND hs_coach IS DISTINCT FROM coach
        """
    )
    rows = cur.fetchall()
    if not rows:
        print("Stage 2: Asana already matches HubSpot, nothing to push")
        return

    print(f"Stage 2: {len(rows)} card(s) differ from HubSpot")
    options = load_coach_options(api_client)
    tasks_api = asana.TasksApi(api_client)

    written = kept_protected = unmapped = errored = 0
    for task_gid, coach_now, hs_coach in rows:
        # Never override a protected non-HubSpot value (Agency Pro, Alliance).
        if is_protected(coach_now):
            kept_protected += 1
            continue

        option_gid = options.get(hs_coach.strip().lower())
        if not option_gid:
            print(f"  No Asana Coach option named '{hs_coach}' (task {task_gid}) — skipping")
            unmapped += 1
            continue

        # multi_enum value is a LIST of option gids; a single-item list replaces the selection.
        body = {"data": {"custom_fields": {ASANA_COACH_FIELD_GID: [option_gid]}}}
        try:
            tasks_api.update_task(body, task_gid, {})
            written += 1
        except Exception as e:
            print(f"  Failed to update task {task_gid}: {e}")
            errored += 1

    print(f"Stage 2 done: {written} written, {kept_protected} protected (kept), "
          f"{unmapped} unmapped, {errored} errored")


# --- Lambda entry point ---

def lambda_handler(event, context):
    print("HubSpot -> Asana coach sync starting")

    api_client = get_asana_client()
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT pg_try_advisory_lock(hashtext('hubspot_to_asana_coach_sync'))")
        if not cur.fetchone()[0]:
            print("Another run is already in progress, exiting")
            return {"statusCode": 200, "body": "Skipped: already running"}

        stage1_hubspot_to_db(conn)
        stage2_db_to_asana(conn, api_client)

        print("HubSpot -> Asana coach sync complete")
        return {"statusCode": 200, "body": "Sync complete"}

    except Exception as e:
        print(f"ERROR during HubSpot->Asana coach sync: {e}")
        raise

    finally:
        conn.close()
