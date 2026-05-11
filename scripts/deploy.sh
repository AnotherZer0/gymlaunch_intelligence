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
    "HubspotToken=${HUBSPOT_TOKEN}"

echo "Setting log retention..."
aws logs put-retention-policy \
  --log-group-name /aws/lambda/gymlaunch-slack-sync \
  --retention-in-days 30
aws logs put-retention-policy \
  --log-group-name /aws/lambda/gymlaunch-asana-sync \
  --retention-in-days 30
aws logs put-retention-policy \
  --log-group-name /aws/lambda/gymlaunch-asana-deep-sync \
  --retention-in-days 30
aws logs put-retention-policy \
  --log-group-name /aws/lambda/gymlaunch-hubspot-sync \
  --retention-in-days 30

echo "Done."
