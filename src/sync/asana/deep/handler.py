"""
Asana deep sync Lambda — subtasks and comments
Runs daily. For every top-level task already in the DB, fetches subtasks
and comments and upserts them. Designed to run after the hourly main sync
has kept top-level tasks fresh.
"""

import hashlib
import json
import os
import ssl
from datetime import datetime, timezone

import asana
import pg8000


AGENCY_BOARD_PROJECT_GID = os.environ["ASANA_PROJECT_GID"]

CF = {
    "gym_name":                 "Gym Name",
    "client_name":              "Client Name",
    "agency_status":            "Agency Status",
    "account_manager":          "Account Manager",
    "media_buyer":              "Media Buyer",
    "coach":                    "Coach",
    "hubspot_company_id":       "Hubspot Company ID",
    "facebook_page_name":       "FB Page Name",
    "facebook_page_id":         "FB Page ID",
    "facebook_ad_account_id":   "FB Ad Account ID",
    "facebook_ad_account_name": "FB Ad Account Name",
    "ghl_location_id":          "GHL Location ID",
    "ads_live_date":            "Actual Live Date",
    "ad_spend_budget_daily":    "Ad Spend Budget Daily",
}

HASH_FIELDS = [
    "hubspot_company_id",
    "facebook_page_name",
    "facebook_page_id",
    "facebook_ad_account_id",
    "facebook_ad_account_name",
    "ghl_location_id",
    "account_manager",
    "media_buyer",
    "agency_status",
    "ads_live_date",
    "ad_spend_budget_daily",
]


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


# --- Custom field extraction ---

def extract_custom_fields(task: dict) -> dict:
    cf_map = {}
    for field in task.get("custom_fields", []):
        name = field.get("name")
        if name:
            cf_map[name] = field.get("display_value")
    return cf_map


def parse_date(value: str | None):
    if not value:
        return None
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def parse_numeric(value: str | None):
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def build_board_row(task_gid: str, cf_map: dict) -> dict:
    return {
        "task_gid":                   task_gid,
        "gym_name":                   cf_map.get(CF["gym_name"]),
        "client_name":                cf_map.get(CF["client_name"]),
        "agency_status":              cf_map.get(CF["agency_status"]),
        "account_manager":            cf_map.get(CF["account_manager"]),
        "media_buyer":                cf_map.get(CF["media_buyer"]),
        "coach":                      cf_map.get(CF["coach"]),
        "hubspot_company_id":         cf_map.get(CF["hubspot_company_id"]),
        "facebook_page_name":         cf_map.get(CF["facebook_page_name"]),
        "facebook_page_id":           cf_map.get(CF["facebook_page_id"]),
        "facebook_ad_account_id":     cf_map.get(CF["facebook_ad_account_id"]),
        "facebook_ad_account_name":   cf_map.get(CF["facebook_ad_account_name"]),
        "hl_sub_account_location_id": cf_map.get(CF["ghl_location_id"]),
        "ads_live_date":              parse_date(cf_map.get(CF["ads_live_date"])),
        "ad_spend_budget_daily":      parse_numeric(cf_map.get(CF["ad_spend_budget_daily"])),
    }


def compute_content_hash(row: dict) -> str:
    parts = []
    field_map = {
        "hubspot_company_id":       row["hubspot_company_id"],
        "facebook_page_name":       row["facebook_page_name"],
        "facebook_page_id":         row["facebook_page_id"],
        "facebook_ad_account_id":   row["facebook_ad_account_id"],
        "facebook_ad_account_name": row["facebook_ad_account_name"],
        "ghl_location_id":          row["hl_sub_account_location_id"],
        "account_manager":          row["account_manager"],
        "media_buyer":              row["media_buyer"],
        "agency_status":            row["agency_status"],
        "ads_live_date":            str(row["ads_live_date"]) if row["ads_live_date"] else "",
        "ad_spend_budget_daily":    str(row["ad_spend_budget_daily"]) if row["ad_spend_budget_daily"] is not None else "",
    }
    for key in HASH_FIELDS:
        parts.append(f"{key}={field_map.get(key) or ''}")
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


# --- Upsert helpers ---

def upsert_user(cur, user: dict) -> None:
    if not user or not user.get("gid"):
        return
    cur.execute(
        """
        INSERT INTO asana_user (gid, name, email, updated_at)
        VALUES (%s, %s, %s, now())
        ON CONFLICT (gid) DO UPDATE SET
            name       = EXCLUDED.name,
            email      = EXCLUDED.email,
            updated_at = now()
        """,
        (user["gid"], user.get("name", ""), user.get("email")),
    )


def upsert_task(cur, task: dict, project_gid: str, parent_gid: str | None = None) -> None:
    assignee = task.get("assignee") or {}
    if assignee.get("gid"):
        upsert_user(cur, assignee)

    completed_at = None
    if task.get("completed_at"):
        try:
            completed_at = datetime.fromisoformat(task["completed_at"].replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            pass

    asana_created_at = None
    if task.get("created_at"):
        try:
            asana_created_at = datetime.fromisoformat(task["created_at"].replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            pass

    asana_modified_at = None
    if task.get("modified_at"):
        try:
            asana_modified_at = datetime.fromisoformat(task["modified_at"].replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            pass

    section_name = None
    memberships = task.get("memberships") or []
    if memberships:
        section_name = (memberships[0].get("section") or {}).get("name")

    due_on = parse_date(task.get("due_on"))

    cur.execute(
        """
        INSERT INTO asana_task (
            gid, project_gid, parent_task_gid, assignee_gid,
            name, notes, section_name, due_on,
            completed, completed_at,
            asana_created_at, asana_modified_at,
            raw_payload, updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
        ON CONFLICT (gid) DO UPDATE SET
            assignee_gid      = EXCLUDED.assignee_gid,
            name              = EXCLUDED.name,
            notes             = EXCLUDED.notes,
            section_name      = EXCLUDED.section_name,
            due_on            = EXCLUDED.due_on,
            completed         = EXCLUDED.completed,
            completed_at      = EXCLUDED.completed_at,
            asana_modified_at = EXCLUDED.asana_modified_at,
            raw_payload       = EXCLUDED.raw_payload,
            updated_at        = now()
        """,
        (
            task["gid"], project_gid, parent_gid, assignee.get("gid"),
            task.get("name", ""), task.get("notes"), section_name, due_on,
            task.get("completed", False), completed_at,
            asana_created_at, asana_modified_at, json.dumps(task),
        ),
    )


def upsert_board_row(cur, row: dict) -> None:
    content_hash = compute_content_hash(row)
    cur.execute(
        """
        INSERT INTO asana_agency_board_task (
            task_gid,
            gym_name, client_name, agency_status,
            account_manager, media_buyer, coach,
            hubspot_company_id,
            facebook_page_name, facebook_page_id,
            facebook_ad_account_id, facebook_ad_account_name,
            hl_sub_account_location_id,
            ads_live_date, ad_spend_budget_daily,
            content_hash, updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
        ON CONFLICT (task_gid) DO UPDATE SET
            gym_name                    = EXCLUDED.gym_name,
            client_name                 = EXCLUDED.client_name,
            agency_status               = EXCLUDED.agency_status,
            account_manager             = EXCLUDED.account_manager,
            media_buyer                 = EXCLUDED.media_buyer,
            coach                       = EXCLUDED.coach,
            hubspot_company_id          = EXCLUDED.hubspot_company_id,
            facebook_page_name          = EXCLUDED.facebook_page_name,
            facebook_page_id            = EXCLUDED.facebook_page_id,
            facebook_ad_account_id      = EXCLUDED.facebook_ad_account_id,
            facebook_ad_account_name    = EXCLUDED.facebook_ad_account_name,
            hl_sub_account_location_id  = EXCLUDED.hl_sub_account_location_id,
            ads_live_date               = EXCLUDED.ads_live_date,
            ad_spend_budget_daily       = EXCLUDED.ad_spend_budget_daily,
            content_hash                = EXCLUDED.content_hash,
            updated_at                  = now()
        """,
        (
            row["task_gid"], row["gym_name"], row["client_name"], row["agency_status"],
            row["account_manager"], row["media_buyer"], row["coach"],
            row["hubspot_company_id"], row["facebook_page_name"], row["facebook_page_id"],
            row["facebook_ad_account_id"], row["facebook_ad_account_name"],
            row["hl_sub_account_location_id"], row["ads_live_date"],
            row["ad_spend_budget_daily"], content_hash,
        ),
    )


def upsert_comment(cur, story: dict, task_gid: str) -> None:
    if not story.get("gid"):
        return
    created_at = None
    if story.get("created_at"):
        try:
            created_at = datetime.fromisoformat(story["created_at"].replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            pass
    if not created_at:
        return

    author = story.get("created_by") or {}
    if author.get("gid"):
        upsert_user(cur, author)

    cur.execute(
        """
        INSERT INTO asana_task_comment (gid, task_gid, author_gid, text, created_at_asana)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (gid) DO UPDATE SET
            text = EXCLUDED.text
        """,
        (story["gid"], task_gid, author.get("gid"), story.get("text"), created_at),
    )


# --- Sync helpers ---

def sync_subtasks(cur, tasks_api, task_gid: str, project_gid: str) -> None:
    try:
        opts = {
            "opt_fields": (
                "gid,name,notes,completed,completed_at,created_at,modified_at,"
                "assignee.gid,assignee.name,assignee.email,"
                "due_on,memberships.section.name,custom_fields.name,custom_fields.display_value"
            )
        }
        subtasks = list(tasks_api.get_subtasks_for_task(task_gid, opts))
    except Exception as e:
        print(f"  Could not fetch subtasks for {task_gid}: {e}")
        return

    for subtask in subtasks:
        upsert_task(cur, subtask, project_gid, task_gid)
        cf_map = extract_custom_fields(subtask)
        upsert_board_row(cur, build_board_row(subtask["gid"], cf_map))


def sync_comments(cur, stories_api, task_gid: str) -> None:
    try:
        opts = {
            "opt_fields": "gid,type,text,created_at,created_by.gid,created_by.name,created_by.email"
        }
        stories = list(stories_api.get_stories_for_task(task_gid, opts))
    except Exception as e:
        print(f"  Could not fetch comments for {task_gid}: {e}")
        return

    for story in stories:
        if story.get("type") == "comment":
            upsert_comment(cur, story, task_gid)


# --- Lambda entry point ---

def lambda_handler(event, context):
    print("Asana deep sync starting")

    api_client = get_asana_client()
    conn = get_db_connection()

    try:
        cur = conn.cursor()

        cur.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (AGENCY_BOARD_PROJECT_GID + "_deep",))
        if not cur.fetchone()[0]:
            print("Another deep sync is already running, exiting")
            return {"statusCode": 200, "body": "Skipped: deep sync already running"}

        tasks_api = asana.TasksApi(api_client)
        stories_api = asana.StoriesApi(api_client)

        cur.execute(
            "SELECT gid FROM asana_task WHERE parent_task_gid IS NULL AND project_gid = %s",
            (AGENCY_BOARD_PROJECT_GID,)
        )
        task_gids = [row[0] for row in cur.fetchall()]
        print(f"Deep syncing {len(task_gids)} tasks")

        for i, task_gid in enumerate(task_gids):
            sync_subtasks(cur, tasks_api, task_gid, AGENCY_BOARD_PROJECT_GID)
            sync_comments(cur, stories_api, task_gid)
            conn.commit()

            if (i + 1) % 100 == 0:
                print(f"  {i + 1} tasks deep synced...")

        print(f"Deep sync complete: {len(task_gids)} tasks processed")
        return {"statusCode": 200, "body": "Deep sync complete"}

    except Exception as e:
        print(f"ERROR during deep sync: {e}")
        raise

    finally:
        conn.close()
