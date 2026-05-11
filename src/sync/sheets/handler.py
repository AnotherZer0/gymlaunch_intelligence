"""
Google Sheets sync Lambda — staff availability
Reads staff_availability view from RDS and overwrites the "DB Sync" tab
in the configured Google Sheet. Runs 4x daily during business hours EST.
"""

import base64
import json
import os
import ssl

import gspread
import pg8000
from google.oauth2.service_account import Credentials


SHEET_ID = os.environ["GOOGLE_SHEET_ID"]
TAB_NAME = "DB Sync"

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


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


def get_sheets_client():
    sa_json = json.loads(base64.b64decode(os.environ["GOOGLE_SERVICE_ACCOUNT_B64"]))
    creds = Credentials.from_service_account_info(sa_json, scopes=SCOPES)
    return gspread.authorize(creds)


# --- Sync ---

def lambda_handler(event, context):
    print("Sheets sync starting")

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT name, capacity, active_count, free_slots FROM staff_availability")
        cols = [desc[0] for desc in cur.description]
        rows = cur.fetchall()
        cur.close()
    finally:
        conn.close()

    print(f"Fetched {len(rows)} rows from staff_availability")

    gc = get_sheets_client()
    sh = gc.open_by_key(SHEET_ID)
    ws = sh.worksheet(TAB_NAME)

    ws.clear()
    ws.update([cols] + [list(row) for row in rows])

    print(f"Sheet updated: {len(rows)} rows written to '{TAB_NAME}'")
    return {"statusCode": 200, "body": f"Updated {len(rows)} rows"}
