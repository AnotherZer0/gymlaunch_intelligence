# Deployment Guide

How to deploy, update, and extend the GymLaunch Intelligence Lambda stack.

---

## What we have

One CloudFormation stack (`gymlaunch-slack-sync`) that contains:
- One Lambda function (`gymlaunch-slack-sync`) — runs hourly via EventBridge
- One IAM role (`gymlaunch-slack-sync`) — scoped by a permissions boundary
- One EventBridge rule — triggers the Lambda on `rate(1 hour)`

The Lambda lives inside the VPC (same network as RDS), reaches the internet through a NAT Gateway, and writes Slack messages into Postgres.

---

## File map

```
infra/template.yaml          — SAM template: defines the Lambda, role, schedule
samconfig.toml               — SAM defaults (stack name, region, S3 bucket, non-secret params)
scripts/deploy.sh            — Full deploy: fetches secrets, builds, deploys
scripts/manage_channels.py   — CLI to add/remove channels from the sync list
src/sync/slack/handler.py    — Lambda code
src/sync/slack/requirements.txt — Python dependencies
infra/iam/gymlaunch-deploy-policy.json   — What the deploy user is allowed to do
infra/iam/gymlaunch-lambda-boundary.json — Max permissions any Lambda role can ever have
```

---

## How to deploy (normal update)

Run from the project root:

```bash
bash scripts/deploy.sh
```

That script:
1. Pulls `DB_PASSWORD` and `SLACK_BOT_TOKEN` from Secrets Manager
2. Runs `sam build` — installs Python dependencies into `.aws-sam/build/SlackSyncFunction/`
3. Runs `sam deploy` — uploads the zip to S3, updates the CloudFormation stack
4. Sets the CloudWatch log group retention to 30 days

**Important:** `samconfig.toml` deliberately has no `template_file` under `[default.deploy.parameters]`. This makes `sam deploy` use the *built* template at `.aws-sam/build/template.yaml` (which includes dependencies), not the raw source. Do not add `template_file` back.

---

## How to update existing Lambda code

Edit `src/sync/slack/handler.py`, then:

```bash
bash scripts/deploy.sh
```

That's it. SAM rebuilds and redeploys automatically.

---

## How to add a new Python dependency

Add it to `src/sync/slack/requirements.txt`, then redeploy:

```bash
bash scripts/deploy.sh
```

---

## How to change the schedule

In `infra/template.yaml`, find the `Events` block under `SlackSyncFunction`:

```yaml
Events:
  HourlySync:
    Type: Schedule
    Properties:
      Schedule: rate(1 hour)   # ← change this
```

Valid values: `rate(1 hour)`, `rate(30 minutes)`, `cron(0 9 * * ? *)` (9am UTC daily), etc.

Then redeploy with `bash scripts/deploy.sh`.

---

## How to add a second Lambda to the same stack

The idea: one CloudFormation stack (`gymlaunch-slack-sync`) can hold multiple Lambda functions. For a new integration (e.g. Asana), you add a new function to the same `infra/template.yaml`.

**Step 1 — Add source code**

Create a new directory:
```
src/sync/asana/
    handler.py
    requirements.txt
```

**Step 2 — Add a new Parameter (if new secrets needed)**

In `infra/template.yaml`, add to the `Parameters` section:
```yaml
AsanaPersonalAccessToken:
  Type: String
  NoEcho: true
```

Add the env var to `Globals` or directly on the new function (see step 3).

**Step 3 — Add the new function to `infra/template.yaml`**

Under `Resources`, add:
```yaml
AsanaSyncFunction:
  Type: AWS::Serverless::Function
  Properties:
    FunctionName: gymlaunch-asana-sync
    CodeUri: ../src/sync/asana/
    Handler: handler.lambda_handler
    Role: !GetAtt SlackSyncRole.Arn          # reuse the same role if permissions are compatible
    Environment:
      Variables:
        ASANA_TOKEN: !Ref AsanaPersonalAccessToken
    VpcConfig:
      SecurityGroupIds:
        - !Ref DbSecurityGroupId
      SubnetIds: !Ref SubnetIds
    Events:
      DailySync:
        Type: Schedule
        Properties:
          Schedule: rate(1 hour)
          Name: gymlaunch-asana-sync-hourly
          Enabled: true
```

**Step 4 — Add the new secret to `scripts/deploy.sh`**

```bash
ASANA_TOKEN=$(aws secretsmanager get-secret-value \
  --secret-id gymlaunch/asana/token \
  --query SecretString --output text \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['token'])")
```

Then add `"AsanaPersonalAccessToken=${ASANA_TOKEN}"` to the `--parameter-overrides` list in the same script.

**Step 5 — Redeploy**

```bash
bash scripts/deploy.sh
```

CloudFormation detects the new resource and creates it without touching the Slack function.

---

## How to add a new IAM permission to a Lambda

Lambdas run under `gymlaunch-slack-sync` role, which is capped by the permissions boundary at `infra/iam/gymlaunch-lambda-boundary.json`.

If a Lambda needs a new AWS permission (e.g. writing to SQS):
1. Add it to `gymlaunch-lambda-boundary.json`
2. Apply the updated boundary policy in the AWS Console: IAM → Policies → `gymlaunch-lambda-boundary` → Edit
3. The role picks it up immediately — no redeploy needed

The boundary is the ceiling. The role managed policies (`AWSLambdaVPCAccessExecutionRole`) are the floor of what's actually granted.

---

## How secrets work

Secrets are stored in AWS Secrets Manager. They are **never** in `samconfig.toml` or committed to git.

`scripts/deploy.sh` fetches them at deploy time and passes them as CloudFormation parameters, which become Lambda environment variables. The Lambda reads them from `os.environ`.

To rotate a secret:
1. Update the value in Secrets Manager
2. Run `bash scripts/deploy.sh` — this re-injects the new value as an env var

---

## How to manage which Slack channels get synced

```bash
# List all channels
python3 scripts/manage_channels.py list

# Add a channel (bot must already be a member)
python3 scripts/manage_channels.py add general

# Deactivate (stops syncing, keeps historical data)
python3 scripts/manage_channels.py deactivate general
```

The script requires Secrets Manager access — run it from a machine with the `gymlaunch-deploy` IAM profile.

---

## How to check if the Lambda ran

**CloudWatch Logs:**
AWS Console → CloudWatch → Log groups → `/aws/lambda/gymlaunch-slack-sync`

Each invocation logs:
- Which channels it found
- The oldest message timestamp it's syncing from
- Any errors per channel

**Check the database:**
```sql
SELECT channel_id, last_ts, last_synced_at, status, error_message
FROM slack_sync_state
ORDER BY last_synced_at DESC;
```

---

## How to trigger the Lambda manually

AWS Console → Lambda → `gymlaunch-slack-sync` → Test tab → create an empty test event `{}` → Run.

Or via CLI:
```bash
aws lambda invoke \
  --function-name gymlaunch-slack-sync \
  --payload '{}' \
  /tmp/response.json && cat /tmp/response.json
```

---

## Stack name and region

- Stack: `gymlaunch-slack-sync`
- Region: `us-east-1`
- S3 artifact bucket: `gymlaunch-sam-artifacts`
- Account: `321763729286`

---

## VPC and networking — what we built and why

This is the part that tripped us up the most. Here's what exists and why every piece is necessary.

### The problem we were solving

The Lambda needs to do two things at the same time:
1. **Talk to RDS** — which is inside your VPC (private network), not exposed to the internet
2. **Talk to the Slack API** — which is on the public internet

These two requirements conflict. By default, a Lambda not in any VPC has full internet access but can't reach your private RDS. A Lambda inside the VPC can reach RDS but has no internet access by default (no public IP, no route out).

The solution is a **NAT Gateway** — a managed AWS service that gives your private Lambda a path to the internet without exposing it directly.

### The pieces and what each one does

**VPC**
Your existing virtual private cloud. RDS lives in here. Think of it as your private office network.

**Security Group `sg-64359563`**
A firewall rule that says "things in this group can talk to other things in this group on port 5432 (Postgres)." Both the RDS instance and the Lambda are assigned to this security group. That's the entire basis of their ability to communicate — no IP addresses involved, just group membership.

**Private subnets: `subnet-a085c381` and `subnet-3d566b33`**
These are "private" because they have no direct route to the internet. The Lambda lives in both of these (AWS uses two subnets across two availability zones for redundancy — if one data center goes down, the other subnet keeps things running). RDS is also in private subnets.

**Public subnet: `subnet-eb1f5bb4`**
This one has a direct route to the internet. The NAT Gateway lives here. It has to be in a public subnet because it needs a real internet connection to forward traffic out.

**Elastic IP**
A static public IP address assigned to the NAT Gateway. This is what the outside world (Slack's API) sees as the source of your Lambda's requests. It doesn't change even if AWS restarts the NAT Gateway underneath. Not strictly required but AWS requires one when creating a NAT Gateway.

**NAT Gateway**
Sits in the public subnet. Receives outbound traffic from the private subnets, forwards it to the internet under its own Elastic IP, and routes responses back. The Lambda never gets a public IP directly — it's always behind the NAT. This is the standard "private subnet with internet access" pattern in AWS.

**Private route table**
A routing rule applied to `subnet-a085c381` and `subnet-3d566b33` that says: "if traffic is headed for the internet (0.0.0.0/0), send it to the NAT Gateway." Without this rule, the Lambda's outbound internet traffic has nowhere to go and the connection hangs.

### The full traffic flow

**Lambda → Slack API:**
Lambda (private subnet) → private route table → NAT Gateway (public subnet) → internet → api.slack.com

**Lambda → RDS:**
Lambda (private subnet) → direct VPC routing → RDS (private subnet)
No NAT involved. They're in the same VPC and share the security group, so they can talk directly.

**EventBridge → Lambda:**
EventBridge is an AWS-managed service. It invokes Lambda via the AWS internal network — no VPC, no internet involved. The EventBridge trigger works regardless of VPC config.

### Why the Lambda only uses two specific subnets

The Lambda is in these two private subnets only:
- `subnet-a085c381` (172.31.80.0/20) — us-east-1b
- `subnet-3d566b33` (172.31.64.0/20) — us-east-1f

Two subnets across two availability zones = redundancy. If one AWS data center has an issue, the Lambda can still run in the other.

`subnet-eb1f5bb4` is the public subnet where the NAT Gateway itself lives — Lambda should never go there. The Lambda doesn't need to be in the same subnet as the NAT, it just needs a route table that points to it, which both private subnets have.

Every future Lambda function uses these same two subnets. The NAT Gateway handles unlimited concurrent Lambdas — you never need to create new subnets for new integrations.

### What breaks if you remove the NAT Gateway

The Lambda will still start and still connect to RDS. But any call to `api.slack.com` (or any external URL) will hang silently until the Lambda's 15-minute timeout kills it. No error, no log output — it just hangs. This is exactly what we saw before adding the NAT.

### What it costs

NAT Gateway pricing is roughly $0.045/hour (~$32/month) plus $0.045/GB of data processed. For an hourly Slack sync, data volume is minimal. The NAT Gateway is the most expensive thing in this stack.

---

## What NOT to do

- Do not add `template_file` back to `samconfig.toml` — it breaks dependency packaging
- Do not add secrets to `samconfig.toml` or `parameter_overrides` — they'd be committed to git
- Do not delete the `gymlaunch-lambda-boundary` policy — it's the security ceiling for all Lambdas
- Do not remove the NAT Gateway routes for `subnet-a085c381` and `subnet-3d566b33` — Lambdas in those subnets need it for internet access (Slack API, etc.)
