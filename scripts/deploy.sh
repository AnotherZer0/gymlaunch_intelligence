#!/usr/bin/env bash
set -euo pipefail

# Fetch secrets from Secrets Manager and deploy the Slack sync Lambda.
# Run from the project root: bash scripts/deploy.sh

echo "Fetching secrets..."

DB_SECRET=$(aws secretsmanager get-secret-value \
  --secret-id gymlaunch/db/gls_writer \
  --query SecretString --output text)
DB_PASSWORD=$(echo "$DB_SECRET" | python3 -c "import sys,json; print(json.load(sys.stdin)['password'])")

SLACK_SECRET=$(aws secretsmanager get-secret-value \
  --secret-id gymlaunch/slack/bot_token \
  --query SecretString --output text)
SLACK_BOT_TOKEN=$(echo "$SLACK_SECRET" | python3 -c "import sys,json; print(json.load(sys.stdin)['bot_token'])")

echo "Building..."
sam build

echo "Deploying..."
sam deploy \
  --parameter-overrides \
    "DbPassword=${DB_PASSWORD}" \
    "SlackBotToken=${SLACK_BOT_TOKEN}"

echo "Done."
