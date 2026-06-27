#!/usr/bin/env bash
set -euo pipefail

# Fetch secrets from Secrets Manager and deploy all sync Lambdas.
# Run from the project root: bash scripts/deploy.sh

echo "Fetching secrets..."

DB_PASSWORD=$(aws secretsmanager get-secret-value \
  --secret-id gymlaunch/db/gls_writer \
  --query SecretString --output text \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['gls_writer'])")

SLACK_BOT_TOKEN=$(aws secretsmanager get-secret-value \
  --secret-id gymlaunch/slack/bot_token \
  --query SecretString --output text \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['bot_token'])")

ASANA_TOKEN=$(aws secretsmanager get-secret-value \
  --secret-id gymlaunch/asana/token \
  --query SecretString --output text \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['token'])")

HUBSPOT_TOKEN=$(aws secretsmanager get-secret-value \
  --secret-id gymlaunch/hubspot/token \
  --query SecretString --output text \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['token'])")

GOOGLE_SERVICE_ACCOUNT_B64=$(aws secretsmanager get-secret-value \
  --secret-id gymlaunch/google/service_account \
  --query SecretString --output text \
  | python3 -c "import sys,base64; print(base64.b64encode(sys.stdin.read().encode()).decode())")

TWILIO_AUTH_TOKEN=$(aws secretsmanager get-secret-value \
  --secret-id gymlaunch/twilio/auth_token \
  --query SecretString --output text \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['auth_token'])")

OCTOPODS_WEBHOOK_URLS=$(aws secretsmanager get-secret-value \
  --secret-id gymlaunch/twilio/octopods_webhook_urls \
  --query SecretString --output text \
  | base64 -w 0)

TWILIO_ACCOUNT_SID=$(aws secretsmanager get-secret-value \
  --secret-id gymlaunch/twilio/account_sid \
  --query SecretString --output text \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['account_sid'])")

FATHOM_WEBHOOK_SECRET=$(aws secretsmanager get-secret-value \
  --secret-id gymlaunch/fathom/Fathom-Webhook-Secret \
  --query SecretString --output text \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['secret'])")

FATHOM_API_KEY=$(aws secretsmanager get-secret-value \
  --secret-id gymlaunch/fathom/Fathom-API-Key \
  --query SecretString --output text \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['api_key'])")

ANTHROPIC_API_KEY=$(aws secretsmanager get-secret-value \
  --secret-id gymlaunch/anthropic/api_key \
  --query SecretString --output text \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['api_key'])")

# JSON object: {"acct_1xxx": "sk_live_xxx", ...} — base64 encoded to survive param quoting
STRIPE_API_KEYS_B64=$(aws secretsmanager get-secret-value \
  --secret-id gymlaunch/stripe/api_keys \
  --query SecretString --output text \
  | base64 -w 0)

# Supabase project URL + service_role JWT, stored as one JSON secret.
SUPABASE_SECRET_JSON=$(aws secretsmanager get-secret-value \
  --secret-id gymlaunch/supabase/api \
  --query SecretString --output text)
SUPABASE_URL=$(echo "$SUPABASE_SECRET_JSON" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['url'])")
SUPABASE_SERVICE_ROLE_KEY=$(echo "$SUPABASE_SECRET_JSON" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['service_role_key'])")
unset SUPABASE_SECRET_JSON

# SubscriptionFlow OAuth2 client-credentials + the shared key callers must present
# to the subscribe Function URL. Stored as one JSON secret:
#   {"client_id": "...", "client_secret": "...", "endpoint_api_key": "..."}
SF_SECRET_JSON=$(aws secretsmanager get-secret-value \
  --secret-id gymlaunch/subscriptionflow/api \
  --query SecretString --output text)
SF_CLIENT_ID=$(echo "$SF_SECRET_JSON" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['client_id'])")
SF_CLIENT_SECRET=$(echo "$SF_SECRET_JSON" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['client_secret'])")
SF_ENDPOINT_API_KEY=$(echo "$SF_SECRET_JSON" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['endpoint_api_key'])")
unset SF_SECRET_JSON

# From address for the daily finance report email (domain must be verified in SES us-east-1)
SES_FROM_ADDRESS="reports@gymlaunch.com"
SES_TO_ADDRESS="ap@gymlaunchsecrets.com"

# Google Sheet that holds the `00 - Database` master client tab. Read by
# gymlaunch-lead_db2-sheet-sync. The service account must have read access on
# this sheet — share it with n8n-sheets-integration@zsign-transfer.iam.gserviceaccount.com.
LEAD_DB2_SHEET_ID="1JGdbjR1g8MF0zzraOwyPNNY7-jRrVIzk2ZZI-FWhky0"

echo "Building..."
sam build

echo "Deploying..."
sam deploy \
  --force-upload \
  --parameter-overrides \
    "DbHost=gls.cdrq9b1h5qzb.us-east-1.rds.amazonaws.com" \
    "DbName=gymlaunch_intelligence" \
    "DbUser=gls_writer" \
    "DbSecurityGroupId=sg-64359563" \
    "SubnetIds=subnet-a085c381,subnet-3d566b33" \
    "DbPassword=${DB_PASSWORD}" \
    "SlackBotToken=${SLACK_BOT_TOKEN}" \
    "AsanaToken=${ASANA_TOKEN}" \
    "HubspotToken=${HUBSPOT_TOKEN}" \
    "GoogleServiceAccountB64=${GOOGLE_SERVICE_ACCOUNT_B64}" \
    "GoogleSheetId=1xp4H9SUHHNgFu9PB_fchFpn8c1JwrF-7u74I3qLe5KY" \
    "TwilioAuthToken=${TWILIO_AUTH_TOKEN}" \
    "OctopodWebhookUrls=${OCTOPODS_WEBHOOK_URLS}" \
    "TwilioAccountSid=${TWILIO_ACCOUNT_SID}" \
    "FathomWebhookSecret=${FATHOM_WEBHOOK_SECRET}" \
    "StripeApiKeysB64=${STRIPE_API_KEYS_B64}" \
    "SesFromAddress=${SES_FROM_ADDRESS}" \
    "SesToAddress=${SES_TO_ADDRESS}" \
    "SupabaseUrl=${SUPABASE_URL}" \
    "SupabaseServiceRoleKey=${SUPABASE_SERVICE_ROLE_KEY}" \
    "LeadDb2SheetId=${LEAD_DB2_SHEET_ID}" \
    "SfClientId=${SF_CLIENT_ID}" \
    "SfClientSecret=${SF_CLIENT_SECRET}" \
    "SfEndpointApiKey=${SF_ENDPOINT_API_KEY}" \
    "FathomApiKey=${FATHOM_API_KEY}" \
    "AnthropicApiKey=${ANTHROPIC_API_KEY}"

echo "Setting log retention..."
set_retention() {
  aws logs create-log-group --log-group-name "$1" 2>/dev/null || true
  aws logs put-retention-policy --log-group-name "$1" --retention-in-days 30
}
set_retention /aws/lambda/gymlaunch-slack-sync
set_retention /aws/lambda/gymlaunch-asana-agency-board-sync
set_retention /aws/lambda/gymlaunch-asana-agency-board-deep-sync
set_retention /aws/lambda/gymlaunch-sync-agency-board-to-hubspot
set_retention /aws/lambda/gymlaunch-sync-hubspot-to-agency-board
set_retention /aws/lambda/gymlaunch-mb-capacity-sheet-sync
set_retention /aws/lambda/gymlaunch-sms-interceptor
set_retention /aws/lambda/gymlaunch-phone-validator
set_retention /aws/lambda/gymlaunch-fathom-webhook
set_retention /aws/lambda/gymlaunch-stripe-finance-report
set_retention /aws/lambda/gymlaunch-project-note-sync
set_retention /aws/lambda/gymlaunch-supabase-lead-sync
set_retention /aws/lambda/gymlaunch-lead_db2-sheet-sync
set_retention /aws/lambda/gymlaunch-sf-create-custom-weekly-sub-for-go-product
set_retention /aws/lambda/gymlaunch-fathom-daily-sync
set_retention /aws/lambda/gymlaunch-client-identity-resolver
set_retention /aws/lambda/gymlaunch-client-pulse-summary
# gymlaunch-add-slack-channel is managed manually (not in the SAM stack — see template.yaml),
# but we still set log retention on it here for hygiene.
set_retention /aws/lambda/gymlaunch-add-slack-channel

echo "Done."
