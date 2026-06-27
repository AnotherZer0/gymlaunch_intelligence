"""
gymlaunch-client-identity-resolver

The first thing to ever WRITE to the identity layer (client_account /
client_external_id, defined back in 001_foundation but never populated). It
builds the "address book" the AI brain uses: one canonical client_account per
owner, with client_external_id rows fanning out to each system's IDs.

It runs daily and is idempotent — every write is an upsert, so re-running just
re-derives the current state from the live sources. There is no manual seed.

WHERE EACH LINK COMES FROM
  asana   — asana_agency_board_task: every top-level card with a gym_name is a
            client project. task_gid -> (asana, task); hubspot_company_id ->
            (hubspot, company).
  slack   — HubSpot company property (Tier 0, on-demand read). HubSpot stores the
            client's Slack channel ID on the company; we batch-read it for the
            companies we already track. Nothing else from HubSpot is persisted.
            channel id -> (slack, channel) + slack_channel.client_account_id.
  fathom  — derived from Slack: the external (is_internal=false) people who post
            in a linked channel are the owner/staff; their slack_user.email
            values are the Fathom keys. email -> (fathom, email).

OWNER GROUPING (one owner, many locations -> one client_account)
  A client with two locations has two cards + two HubSpot companies but one
  shared Slack channel. We union-find cards together when they share the same
  HubSpot Slack-channel value (primary, strong) OR the same client_name
  (fallback, weaker). Each cluster becomes one client_account.

CHURN (soft-deactivate, keep history)
  A client with no live (uncompleted, still-present) Asana card is flagged
  client_account.active = false. Its identity rows and past summaries are kept;
  the pulse Lambda skips inactive clients. Reappearing reactivates it.

CONFIG (env)
  SLACK_CHANNEL_PROPERTY   optional. HubSpot company-property internal name that
                           holds the Slack channel. If unset, auto-discovered
                           from the portal (and the choice is logged so it can be
                           pinned here).
  DEFAULT_ORGANIZATION_ID  optional. organization.id to stamp on new
                           client_account rows. Defaults to the only org row if
                           there's exactly one, else NULL.
"""

import os
import ssl
import time

import pg8000
import requests

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


def hubspot_headers():
    return {
        "Authorization": f"Bearer {os.environ['HUBSPOT_TOKEN']}",
        "Content-Type": "application/json",
    }


def _handle_rate_limit(resp: requests.Response) -> None:
    if resp.status_code == 429:
        wait = int(resp.headers.get("Retry-After", 10))
        print(f"  Rate limited by HubSpot, waiting {wait}s")
        time.sleep(wait)


# --- HubSpot: which company property holds the Slack channel? ---

def discover_channel_property() -> str | None:
    """
    Return the internal name of the HubSpot company property that stores the
    Slack channel. Honors SLACK_CHANNEL_PROPERTY if set; otherwise inspects the
    company property schema and picks the best 'slack'/'channel' match, logging
    all candidates so the winner can be pinned via the env var.
    """
    override = os.environ.get("SLACK_CHANNEL_PROPERTY", "").strip()
    if override:
        print(f"Using SLACK_CHANNEL_PROPERTY override: {override}")
        return override

    url = f"{HUBSPOT_BASE_URL}/crm/v3/properties/companies"
    resp = requests.get(url, headers=hubspot_headers(), timeout=REQUEST_TIMEOUT_SECONDS)
    _handle_rate_limit(resp)
    if resp.status_code == 429:
        resp = requests.get(url, headers=hubspot_headers(), timeout=REQUEST_TIMEOUT_SECONDS)
    if resp.status_code != 200:
        print(f"  Could not list company properties: {resp.status_code} {resp.text[:300]}")
        return None

    candidates = []
    for p in resp.json().get("results", []):
        name = (p.get("name") or "")
        label = (p.get("label") or "")
        hay = f"{name} {label}".lower()
        if "slack" in hay:
            # Prefer ones that also mention "channel".
            score = 2 if "channel" in hay else 1
            candidates.append((score, name, label))

    if not candidates:
        print("  No 'slack' company property found — Slack/Fathom linking skipped this run. "
              "Set SLACK_CHANNEL_PROPERTY once you know the internal name.")
        return None

    candidates.sort(reverse=True)
    chosen = candidates[0][1]
    listing = ", ".join(f"{n} ({l!r})" for _, n, l in candidates)
    print(f"Auto-discovered Slack channel property: '{chosen}'. Candidates: {listing}. "
          f"Pin it via SLACK_CHANNEL_PROPERTY to skip discovery.")
    return chosen


def batch_read_property(company_ids: list, prop: str) -> dict:
    """Batch-read one property for the given company ids. Returns {company_id: value_or_None}."""
    values = {}
    url = f"{HUBSPOT_BASE_URL}/crm/v3/objects/companies/batch/read"
    for i in range(0, len(company_ids), HUBSPOT_BATCH_SIZE):
        chunk = company_ids[i : i + HUBSPOT_BATCH_SIZE]
        payload = {"properties": [prop], "inputs": [{"id": c} for c in chunk]}
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
            raw = (result.get("properties") or {}).get(prop)
            values[cid] = raw.strip() if isinstance(raw, str) and raw.strip() else None
    return values


# --- Union-find for owner grouping ---

class UnionFind:
    def __init__(self):
        self.parent = {}

    def add(self, x):
        self.parent.setdefault(x, x)

    def find(self, x):
        self.add(x)
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:   # path compression
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a, b):
        self.parent[self.find(a)] = self.find(b)


def _norm_name(s) -> str:
    return " ".join((s or "").split()).strip().lower()


def _placeholders(n: int) -> str:
    return ",".join(["%s"] * n)


# --- DB helpers ---

def get_default_org_id(cur):
    env = os.environ.get("DEFAULT_ORGANIZATION_ID", "").strip()
    if env:
        return int(env)
    cur.execute("SELECT id FROM organization ORDER BY id LIMIT 1")
    row = cur.fetchone()
    return row[0] if row else None


def resolve_channel(cur, raw: str):
    """
    Map a HubSpot-stored channel value to a registered slack_channel.
    Returns (channel_id_or_None, value_to_store). Accepts a channel id (normal
    case) or, defensively, a channel name; stores the resolved id when the
    channel is registered, else the raw value (and logs that it isn't synced).
    """
    val = raw.strip()
    cur.execute("SELECT channel_id FROM slack_channel WHERE channel_id = %s", (val,))
    row = cur.fetchone()
    if row:
        return row[0], row[0]
    name = val[1:] if val.startswith("#") else val
    cur.execute("SELECT channel_id FROM slack_channel WHERE name = %s", (name,))
    row = cur.fetchone()
    if row:
        return row[0], row[0]
    return None, val


def find_existing_account(cur, task_gids: list, company_ids: list):
    """Reuse an existing client_account if any of this group's IDs already point to one."""
    clauses, params = [], []
    if task_gids:
        clauses.append(f"(system='asana' AND id_type='task' AND value IN ({_placeholders(len(task_gids))}))")
        params.extend(task_gids)
    if company_ids:
        clauses.append(f"(system='hubspot' AND id_type='company' AND value IN ({_placeholders(len(company_ids))}))")
        params.extend(company_ids)
    if not clauses:
        return None
    cur.execute(
        "SELECT DISTINCT client_account_id FROM client_external_id WHERE " + " OR ".join(clauses),
        params,
    )
    ids = sorted(r[0] for r in cur.fetchall())
    if not ids:
        return None
    if len(ids) > 1:
        print(f"  WARNING: group maps to multiple existing client_accounts {ids}; "
              f"reusing min {ids[0]} (manual review may be needed)")
    return ids[0]


def upsert_external_id(cur, account_id: int, system: str, id_type: str, value: str):
    cur.execute(
        """
        INSERT INTO client_external_id (client_account_id, system, id_type, value)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (system, id_type, value)
        DO UPDATE SET client_account_id = EXCLUDED.client_account_id
        """,
        (account_id, system, id_type, value),
    )


# --- Resolver core ---

def resolve(conn) -> None:
    cur = conn.cursor()

    # 1. Live client cards from the Asana agency board (top-level, has a gym name).
    cur.execute(
        """
        SELECT ab.task_gid, ab.hubspot_company_id, ab.client_name, ab.gym_name, t.completed
        FROM asana_agency_board_task ab
        JOIN asana_task t ON t.gid = ab.task_gid
        WHERE t.parent_task_gid IS NULL
          AND ab.gym_name IS NOT NULL
        """
    )
    cards = [
        {"task_gid": r[0], "company_id": r[1], "client_name": r[2],
         "gym_name": r[3], "completed": bool(r[4])}
        for r in cur.fetchall()
    ]
    if not cards:
        print("No agency-board client cards found; nothing to resolve.")
        return
    print(f"Loaded {len(cards)} client card(s) from the agency board.")

    # 2. HubSpot: read the Slack-channel property for the companies we track.
    company_ids = sorted({c["company_id"] for c in cards if c["company_id"]})
    company_channel = {}
    prop = discover_channel_property()
    if prop and company_ids:
        company_channel = batch_read_property(company_ids, prop)
        linked = sum(1 for v in company_channel.values() if v)
        print(f"Read '{prop}' for {len(company_ids)} companies; {linked} have a channel value.")
    for c in cards:
        c["channel_raw"] = company_channel.get(c["company_id"]) if c["company_id"] else None

    # 3. Owner grouping: union cards by shared channel value (primary) or client_name (fallback).
    uf = UnionFind()
    by_channel, by_name = {}, {}
    for c in cards:
        uf.add(c["task_gid"])
        if c["channel_raw"]:
            by_channel.setdefault(c["channel_raw"], []).append(c["task_gid"])
        nm = _norm_name(c["client_name"])
        if nm:
            by_name.setdefault(nm, []).append(c["task_gid"])
    for members in list(by_channel.values()) + list(by_name.values()):
        for other in members[1:]:
            uf.union(members[0], other)

    groups = {}
    for c in cards:
        groups.setdefault(uf.find(c["task_gid"]), []).append(c)
    print(f"Grouped into {len(groups)} owner(s).")

    org_id = get_default_org_id(cur)
    active_ids, stats = set(), {"created": 0, "reused": 0, "channels": 0, "emails": 0}

    # 4. Per owner: resolve a client_account and upsert its external IDs.
    for members in groups.values():
        task_gids = [m["task_gid"] for m in members]
        group_company_ids = sorted({m["company_id"] for m in members if m["company_id"]})
        name = next((m["client_name"] for m in members if m["client_name"]),
                    None) or next((m["gym_name"] for m in members if m["gym_name"]), "Unknown")

        account_id = find_existing_account(cur, task_gids, group_company_ids)
        if account_id is None:
            cur.execute(
                "INSERT INTO client_account (organization_id, name) VALUES (%s, %s) RETURNING id",
                (org_id, name),
            )
            account_id = cur.fetchone()[0]
            stats["created"] += 1
        else:
            stats["reused"] += 1
            cur.execute(
                "UPDATE client_account SET name = %s, updated_at = now() "
                "WHERE id = %s AND name IS DISTINCT FROM %s",
                (name, account_id, name),
            )

        for m in members:
            upsert_external_id(cur, account_id, "asana", "task", m["task_gid"])
            if m["company_id"]:
                upsert_external_id(cur, account_id, "hubspot", "company", m["company_id"])

        # Slack channels for this owner (resolved to registered channel_ids where possible).
        resolved_channels = []        # registered channel_ids (for slack_channel + fathom)
        channel_values = []           # every value we stored this run (for stale cleanup)
        for raw in sorted({m["channel_raw"] for m in members if m["channel_raw"]}):
            channel_id, store_val = resolve_channel(cur, raw)
            upsert_external_id(cur, account_id, "slack", "channel", store_val)
            channel_values.append(store_val)
            stats["channels"] += 1
            if channel_id:
                cur.execute(
                    "UPDATE slack_channel SET client_account_id = %s, updated_at = now() "
                    "WHERE channel_id = %s",
                    (account_id, channel_id),
                )
                resolved_channels.append(channel_id)
            else:
                print(f"  Channel value {store_val!r} for account {account_id} isn't a registered "
                      f"slack_channel yet — recorded the link; messages will appear once it's synced.")

        # Retire stale slack channels ONLY when HubSpot gave us a channel this run
        # (a transient empty read shouldn't unlink a client). Keep all current
        # values, registered or not.
        if channel_values:
            cur.execute(
                "DELETE FROM client_external_id "
                f"WHERE client_account_id = %s AND system='slack' AND id_type='channel' "
                f"AND value NOT IN ({_placeholders(len(channel_values))})",
                [account_id, *channel_values],
            )

        # Fathom keys: external posters' emails in this owner's registered channels.
        if resolved_channels:
            cur.execute(
                "SELECT DISTINCT lower(su.email) "
                "FROM slack_message m JOIN slack_user su ON su.user_id = m.user_id "
                f"WHERE m.channel_id IN ({_placeholders(len(resolved_channels))}) "
                "AND su.is_internal = false AND su.email IS NOT NULL AND su.email <> ''",
                resolved_channels,
            )
            for (email,) in cur.fetchall():
                upsert_external_id(cur, account_id, "fathom", "email", email)
                stats["emails"] += 1

        if any(not m["completed"] for m in members):
            active_ids.add(account_id)

    # 5. Churn: reactivate clients with a live card, soft-deactivate the rest of our
    #    managed (asana-linked) universe.
    cur.execute("SELECT DISTINCT client_account_id FROM client_external_id WHERE system='asana'")
    managed = {r[0] for r in cur.fetchall()}
    if active_ids:
        cur.execute(
            f"UPDATE client_account SET active=true, updated_at=now() "
            f"WHERE NOT active AND id IN ({_placeholders(len(active_ids))})",
            list(active_ids),
        )
    to_deactivate = managed - active_ids
    if to_deactivate:
        cur.execute(
            f"UPDATE client_account SET active=false, updated_at=now() "
            f"WHERE active AND id IN ({_placeholders(len(to_deactivate))})",
            list(to_deactivate),
        )

    conn.commit()
    print(f"Done: {stats['created']} created, {stats['reused']} reused; "
          f"{stats['channels']} slack links, {stats['emails']} fathom emails; "
          f"{len(active_ids)} active, {len(to_deactivate)} deactivated.")


# --- Lambda entry point ---

def lambda_handler(event, context):
    print("Client identity resolver starting")
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT pg_try_advisory_lock(hashtext('client_identity_resolver'))")
        if not cur.fetchone()[0]:
            print("Another run is already in progress, exiting")
            return {"statusCode": 200, "body": "Skipped: already running"}

        resolve(conn)
        print("Client identity resolver complete")
        return {"statusCode": 200, "body": "Resolve complete"}
    except Exception as e:
        print(f"ERROR during identity resolve: {e}")
        raise
    finally:
        conn.close()
