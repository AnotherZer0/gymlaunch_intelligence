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

# JSON object: {"acct_1xxx": "sk_live_xxx", ...} — base64 encoded to survive param quoting
STRIPE_API_KEYS_B64=$(aws secretsmanager get-secret-value \
  --secret-id gymlaunch/stripe/api_keys \
  --query SecretString --output text \
  | base64 -w 0)

# From address for the daily finance report email (domain must be verified in SES us-east-1)
SES_FROM_ADDRESS="reports@gymlaunch.com"
SES_TO_ADDRESS="ap@gymlaunchsecrets.com"

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
    "SesToAddress=${SES_TO_ADDRESS}"

echo "Setting log retention..."
set_retention() {
  aws logs create-log-group --log-group-name "$1" 2>/dev/null || true
  aws logs put-retention-policy --log-group-name "$1" --retention-in-days 30
}
set_retention /aws/lambda/gymlaunch-slack-sync
set_retention /aws/lambda/gymlaunch-asana-agency-board-sync
set_retention /aws/lambda/gymlaunch-asana-agency-board-deep-sync
set_retention /aws/lambda/gymlaunch-sync-agency-board-to-hubspot
set_retention /aws/lambda/gymlaunch-mb-capacity-sheet-sync
set_retention /aws/lambda/gymlaunch-sms-interceptor
set_retention /aws/lambda/gymlaunch-phone-validator
set_retention /aws/lambda/gymlaunch-fathom-webhook
set_retention /aws/lambda/gymlaunch-stripe-finance-report

echo "Done."
