GOOGLE SHEETS SYNC — STAFF AVAILABILITY
========================================

WHAT THIS DOES
--------------
This Lambda reads the staff_availability view from the RDS database and
overwrites the "DB Sync" tab in the configured Google Sheet with the
latest data. It runs automatically 4 times a day during business hours.

The staff_availability view shows each media buyer's capacity, how many
active clients they currently have, and how many free slots remain. The
data comes from Asana via the hourly Asana sync Lambda, so the sheet
always reflects the latest state from Asana within an hour.


SCHEDULE
--------
Runs at 8am, 12pm, 4pm, and 8pm EDT (UTC 12:00, 16:00, 20:00, 00:00).
In winter (EST) this shifts one hour earlier due to daylight saving time.


HOW IT WORKS
------------
1. Lambda connects to RDS and runs: SELECT * FROM staff_availability
2. Authenticates to Google Sheets using a service account
3. Clears the "DB Sync" tab entirely
4. Writes the column headers and all rows fresh

The sheet is fully overwritten on every run — nothing is appended or
merged. Whatever is in the DB at that moment is what ends up in the sheet.


CREDENTIALS
-----------
The Google service account JSON is stored in AWS Secrets Manager at:
    gymlaunch/google/service_account

It is fetched at deploy time, base64 encoded, and passed to the Lambda
as an environment variable. You never need to touch the JSON file after
the initial setup.

The service account must have Editor access to the Google Sheet. If the
sheet is ever re-created or the sharing is removed, re-share it with the
client_email address found inside the service account JSON.


GOOGLE SHEET
------------
Sheet ID: 1xp4H9SUHHNgFu9PB_fchFpn8c1JwrF-7u74I3qLe5KY
Tab:      DB Sync

To point this at a different sheet, update the GoogleSheetId value in
scripts/deploy.sh and redeploy. To use a different tab name, update
TAB_NAME in handler.py and redeploy.


ADDING A NEW VIEW OR CHANGING WHAT GETS SYNCED
-----------------------------------------------
1. Update the query in handler.py:
       cur.execute("SELECT * FROM staff_availability")
   Replace staff_availability with any view or query you want.

2. Redeploy: bash scripts/deploy.sh

The column headers in the sheet are pulled automatically from the query
result — no hardcoding needed.


RUNNING IT MANUALLY
-------------------
From the AWS Lambda console, find gymlaunch-mb-capacity-sheet-sync, click Test,
create a test event with an empty JSON body {}, and run it. The sheet
will update immediately.
