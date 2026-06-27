"""
gymlaunch-client-pulse-summary

Bi-weekly "pulse check": for each allowlisted, active client, gather the last 14
days of Slack messages, Asana tickets, and Fathom calls — resolved through the
identity layer (client_external_id) that gymlaunch-client-identity-resolver
populates — and have Claude write a short summary of the client's experience.
The summary is stored in client_period_summary (no delivery yet).

Because a client_account collapses an owner's locations into one, the summary is
owner-level (both locations together), with per-location notes where their
Asana status differs.

CONFIG (env)
  ANTHROPIC_API_KEY               required. Claude API key.
  ALLOWLIST_CLIENT_ACCOUNT_IDS    required. Comma-separated client_account ids to
                                  run on (the "few test clients"). Intersected
                                  with client_account.active = true.
  WINDOW_DAYS                     optional, default 14.
  PULSE_MODEL                     optional, default claude-opus-4-8.
"""

import json
import os
import ssl
from datetime import datetime, timedelta, timezone

import anthropic
import pg8000

DEFAULT_MODEL    = "claude-opus-4-8"
DEFAULT_WINDOW   = 14
MAX_TOKENS       = 16000          # room for adaptive thinking + the summary; non-streaming safe
PER_CALL_TRANSCRIPT_CAP = 60000   # chars; defensive cap per Fathom transcript

SYSTEM_PROMPT = (
    "You are an internal analyst at a gym-marketing agency. You read a client's "
    "recent Slack messages, Asana project activity, and Fathom call transcripts "
    "and write a concise 'pulse check' on the client's experience for the account "
    "team. Be direct and specific; cite concrete events. Cover: overall sentiment, "
    "momentum and wins, open issues or blockers, and any churn/risk signals. If the "
    "client has multiple locations, give an owner-level read and add a short "
    "per-location note only where their status meaningfully differs. Do not invent "
    "facts; if a source is empty, say so briefly. Keep it skimmable."
)


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


def get_allowlist() -> list:
    raw = os.environ.get("ALLOWLIST_CLIENT_ACCOUNT_IDS", "")
    return [int(x) for x in raw.replace(" ", "").split(",") if x]


def _placeholders(n: int) -> str:
    return ",".join(["%s"] * n)


# --- Gather ---

def fetch_external(cur, account_id: int) -> dict:
    cur.execute(
        "SELECT system, id_type, value FROM client_external_id WHERE client_account_id = %s",
        (account_id,),
    )
    out = {}
    for system, id_type, value in cur.fetchall():
        out.setdefault((system, id_type), []).append(value)
    return out


def gather_slack(cur, account_id: int, start) -> list:
    # ai_readable_messages already filters out non-indexable / sensitive channels.
    cur.execute(
        """
        SELECT posted_at, user_name, is_internal, text
        FROM ai_readable_messages
        WHERE client_account_id = %s AND posted_at >= %s AND text IS NOT NULL AND text <> ''
        ORDER BY posted_at
        """,
        (account_id, start),
    )
    return cur.fetchall()


def gather_asana(cur, task_gids: list, start) -> dict:
    if not task_gids:
        return {"context": [], "comments": [], "movement": []}
    ph = _placeholders(len(task_gids))

    cur.execute(
        f"""
        SELECT gym_name, client_name, agency_status, account_manager, media_buyer
        FROM asana_agency_board_task WHERE task_gid IN ({ph})
        """,
        task_gids,
    )
    context = cur.fetchall()

    # Card gids + their direct subtasks form the activity scope.
    cur.execute(f"SELECT gid FROM asana_task WHERE parent_task_gid IN ({ph})", task_gids)
    all_gids = task_gids + [r[0] for r in cur.fetchall()]
    ph_all = _placeholders(len(all_gids))

    cur.execute(
        f"""
        SELECT t.name, u.name, c.created_at_asana, c.text
        FROM asana_task_comment c
        JOIN asana_task t ON t.gid = c.task_gid
        LEFT JOIN asana_user u ON u.gid = c.author_gid
        WHERE c.task_gid IN ({ph_all}) AND c.created_at_asana >= %s
        ORDER BY c.created_at_asana
        """,
        [*all_gids, start],
    )
    comments = cur.fetchall()

    cur.execute(
        f"""
        SELECT name, section_name, completed, asana_modified_at
        FROM asana_task
        WHERE gid IN ({ph_all}) AND asana_modified_at >= %s
        ORDER BY asana_modified_at
        """,
        [*all_gids, start],
    )
    movement = cur.fetchall()
    return {"context": context, "comments": comments, "movement": movement}


def gather_fathom(cur, emails: list, start) -> list:
    if not emails:
        return []
    ph = _placeholders(len(emails))
    cur.execute(
        f"""
        SELECT DISTINCT fc.id, fc.meeting_title, fc.meeting_scheduled_start_time,
                        fc.summary, fc.transcript_plaintext
        FROM fathom_call fc
        JOIN fathom_call_invitee fci ON fci.call_id = fc.id
        WHERE lower(fci.email) IN ({ph})
          AND fc.meeting_scheduled_start_time >= %s
        ORDER BY fc.meeting_scheduled_start_time
        """,
        [*[e.lower() for e in emails], start],
    )
    return cur.fetchall()


# --- Prompt + model ---

def build_prompt(name, period_start, period_end, slack, asana, fathom) -> str:
    parts = [
        f"# Client: {name}",
        f"Window: {period_start} to {period_end}\n",
        f"## Slack messages ({len(slack)})",
    ]
    for posted_at, user_name, is_internal, text in slack:
        who = f"{user_name or 'unknown'}{' (internal)' if is_internal else ' (client)'}"
        parts.append(f"[{posted_at:%Y-%m-%d %H:%M}] {who}: {text}")

    parts.append(f"\n## Asana context")
    for gym_name, client_name, agency_status, am, mb in asana["context"]:
        parts.append(f"- {gym_name or client_name}: status={agency_status}, AM={am}, MB={mb}")
    parts.append(f"\n## Asana comments ({len(asana['comments'])})")
    for task_name, author, created, text in asana["comments"]:
        parts.append(f"[{created:%Y-%m-%d}] ({task_name}) {author or 'unknown'}: {text}")
    parts.append(f"\n## Asana task movement ({len(asana['movement'])})")
    for tname, section, completed, modified in asana["movement"]:
        flag = " [completed]" if completed else ""
        parts.append(f"[{modified:%Y-%m-%d}] {tname} -> {section}{flag}")

    parts.append(f"\n## Fathom calls ({len(fathom)})")
    for _id, title, start_time, summary, transcript in fathom:
        parts.append(f"\n### {title or 'Call'} ({start_time:%Y-%m-%d})")
        if summary:
            parts.append(f"Summary: {summary}")
        if transcript:
            t = transcript[:PER_CALL_TRANSCRIPT_CAP]
            if len(transcript) > PER_CALL_TRANSCRIPT_CAP:
                t += "\n…[transcript truncated]"
            parts.append(t)

    parts.append("\n---\nWrite the pulse check now.")
    return "\n".join(parts)


def summarize(prompt: str, model: str) -> str:
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    resp = client.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        thinking={"type": "adaptive"},
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    return next((b.text for b in resp.content if b.type == "text"), "").strip()


def upsert_summary(cur, account_id, period_start, period_end, body, counts, model):
    cur.execute(
        """
        INSERT INTO client_period_summary
            (client_account_id, period_start, period_end, body, source_counts, model)
        VALUES (%s, %s, %s, %s, %s::jsonb, %s)
        ON CONFLICT (client_account_id, period_start, period_end)
        DO UPDATE SET body = EXCLUDED.body,
                      source_counts = EXCLUDED.source_counts,
                      model = EXCLUDED.model,
                      created_at = now()
        """,
        (account_id, period_start, period_end, body, json.dumps(counts), model),
    )


# --- Lambda entry point ---

def lambda_handler(event, context):
    allowlist = get_allowlist()
    if not allowlist:
        print("ALLOWLIST_CLIENT_ACCOUNT_IDS is empty; nothing to do.")
        return {"statusCode": 200, "body": "No clients in allowlist"}

    model = os.environ.get("PULSE_MODEL", DEFAULT_MODEL)
    window = int(os.environ.get("WINDOW_DAYS", DEFAULT_WINDOW))
    period_end = datetime.now(timezone.utc).date()
    period_start = period_end - timedelta(days=window)
    print(f"Pulse window {period_start} -> {period_end}; model {model}; "
          f"allowlist {allowlist}")

    conn = get_db_connection()
    written = skipped = 0
    try:
        cur = conn.cursor()
        ph = _placeholders(len(allowlist))
        cur.execute(
            f"SELECT id, name FROM client_account WHERE active AND id IN ({ph})",
            allowlist,
        )
        clients = cur.fetchall()
        inactive = set(allowlist) - {c[0] for c in clients}
        if inactive:
            print(f"Skipping inactive/unknown client ids: {sorted(inactive)}")

        for account_id, name in clients:
            ext = fetch_external(cur, account_id)
            task_gids = ext.get(("asana", "task"), [])
            emails = ext.get(("fathom", "email"), [])

            slack = gather_slack(cur, account_id, period_start)
            asana = gather_asana(cur, task_gids, period_start)
            fathom = gather_fathom(cur, emails, period_start)

            counts = {
                "slack": len(slack),
                "asana": len(asana["comments"]) + len(asana["movement"]),
                "fathom": len(fathom),
            }
            total = counts["slack"] + counts["asana"] + counts["fathom"]
            if total == 0:
                body = "No Slack, Asana, or Fathom activity in this window."
                used_model = None
                print(f"  {name} (#{account_id}): no activity")
            else:
                prompt = build_prompt(name, period_start, period_end, slack, asana, fathom)
                body = summarize(prompt, model)
                used_model = model
                print(f"  {name} (#{account_id}): {counts}")

            upsert_summary(cur, account_id, period_start, period_end, body, counts, used_model)
            conn.commit()
            written += 1

        print(f"Pulse complete: {written} summaries written, {skipped} skipped.")
        return {"statusCode": 200, "body": f"{written} summaries written"}
    except Exception as e:
        print(f"ERROR during pulse summary: {e}")
        raise
    finally:
        conn.close()
