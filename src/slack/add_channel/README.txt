gymlaunch-add-slack-channel — Function URL endpoint
===================================================

WHAT IT DOES
------------
Registers a Slack channel into the sync database (slack_channel, active=true)
so the hourly Slack sync (gymlaunch-slack-sync) starts pulling its history.
Add-only — there is no list/deactivate here (use scripts/manage_channels.py
for those, or the DB directly).

Built to be driven by HubSpot: a workflow / custom-code action fires a Slack
channel id at the Function URL when a property is updated, and maps the
short text response back into a single-line-text property.


ENDPOINT
--------
A Lambda Function URL (AuthType: NONE). The exact URL is a CloudFormation
output after deploy:  AddSlackChannelUrl
(also visible in the Lambda console > gymlaunch-add-slack-channel > Configuration
 > Function URL). It looks like https://<id>.lambda-url.us-east-1.on.aws/


AUTH (shared secret)
--------------------
Every request must present the secret, matching CHANNEL_ADD_API_KEY, as EITHER:
  - header:        x-api-key: <secret>
  - query string:  ?key=<secret>

The secret lives in Secrets Manager at gymlaunch/slack/channel_add_key as JSON:
  { "api_key": "<the secret>" }
deploy.sh fetches it and passes it to the Lambda. The deploy IAM user cannot
create secrets, so create this secret in the console BEFORE the first deploy.

To generate a secret:
  python3 -c "import secrets; print(secrets.token_urlsafe(32))"

To rotate: put a new value in the secret, redeploy, and update HubSpot.


INPUT (the channel id, any one of)
----------------------------------
  - query string:  ?channel=C0123456789   (or ?channel_id=)
  - JSON body:     {"channel": "C0123456789"}   (or channel_id)
  - raw body:      C0123456789

Must be a Slack channel ID (starts with C/G), not a name. The bot must already
be a member of the channel — this endpoint does NOT auto-join. If it isn't a
member, nothing is registered and the response says to invite the bot.


RESPONSE
--------
Always HTTP 200, body is a single line of <= 256 chars (so HubSpot can map it
straight into a single-line-text property). Examples:
  OK: #client-acme (C0123456789) registered for syncing
  ERROR: bot not in #client-acme (C0123456789) — invite the bot, then retry
  ERROR: cannot read C0123456789: channel_not_found
  ERROR: unauthorized (bad or missing key)
  ERROR: no channel id provided

Outcome is in the text prefix (OK: / ERROR:). If you'd prefer real HTTP error
codes instead of 200-always, change the status args in reply() in handler.py.


HUBSPOT SETUP (outline)
-----------------------
1. Store the channel id on the company/contact in a property.
2. In the workflow, on that property's update, call the Function URL:
     - method POST
     - header  x-api-key: <secret>     (or append ?key=<secret> to the URL)
     - body    {"channel": "<the property value>"}
3. Map the response body into a single-line-text property to see the result.


HOW TO TEST (curl)
------------------
  curl -s -X POST "https://<id>.lambda-url.us-east-1.on.aws/?channel=C0123456789" \
       -H "x-api-key: <secret>"

  # or with the key + channel both in the URL:
  curl -s "https://<id>.lambda-url.us-east-1.on.aws/?key=<secret>&channel=C0123456789"


DEPLOY
------
1. Create the secret gymlaunch/slack/channel_add_key (see AUTH) — one time.
2. bash scripts/deploy.sh
The deploy user cannot delete Lambdas, so a rename would orphan the old one —
delete it manually in the console if you ever rename this.
