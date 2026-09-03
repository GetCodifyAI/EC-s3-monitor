# Runbook — S3 feed freshness monitor

**Stack** `s3-feed-freshness` · **Region** us-east-2 · **Account** 057311931122
**Alerts to** #dam-alerts via the platform incoming webhook
**Owner** data-eng

Alerts when a vendor feed prefix in `cut-dry-vendor-integration` has received no
new `.csv` for its threshold (default 3 business days). Silent on a clean run.

Same shape as the RDS zero-connections and EC2 over-provisioned monitors:
CloudFormation template, daily EventBridge schedule, plain-text Slack through
the platform webhook, dry-run first.

---

## What it does

1. For each feed in the `FeedsJson` parameter, lists the prefix and takes the
   newest `.csv` by `LastModified`.
2. Computes days idle — Mon–Fri only when `BusinessDaysOnly` is `true`.
3. Reads a watermark from SSM to see whether each feed was already known stale.
4. Posts **one aggregated message** covering every newly-stale feed, plus a
   separate recovery message for any feed that started flowing again.
5. Writes the watermark back.

It reports both the per-feed timestamp and the newest file across all feeds.

## What it deliberately does not do

- **No `s3:GetObject`.** Listing metadata only — it cannot read vendor data.
- **No writes to the monitored bucket.** The watermark lives in SSM precisely
  because writing an object into `cut-dry-vendor-integration` would fire that
  bucket's `ObjectCreated` notification into the Snowpipe SNS topic.
- **No change to the bucket's notification config.** The existing EventBridge
  config and `trigger_sync` trigger are untouched.
- **Detects absence, not correctness.** A truncated file resets the clock.
  Zero-byte objects are skipped; row-count validation belongs elsewhere.
- **Detects S3 arrival, not Snowflake load.** A file landing while Snowpipe is
  broken looks healthy here.

---

## First deployment

### 1. Confirm the webhook parameter

The Lambda reads the platform webhook from an SSM SecureString. Confirm the name
and that it posts to #dam-alerts:

```bash
aws ssm get-parameter --name /platform/slack/webhook-dam-alerts \
  --with-decryption --region us-east-2 --query 'Parameter.Value' --output text
```

If the platform webhook lives under a different path, pass
`SLACK_WEBHOOK_PARAM=/your/path` to `deploy.sh`.

### 2. Deploy in dry-run

```bash
cd cfn
./deploy.sh <cfn-artifact-bucket>
```

`DryRun` defaults to `true`: the Lambda logs the exact message it would post and
does not write the watermark. Nothing reaches Slack.

### 3. Read the output

```bash
FN=$(aws cloudformation describe-stacks --stack-name s3-feed-freshness \
  --region us-east-2 --query 'Stacks[0].Outputs[?OutputKey==`FunctionName`].OutputValue' \
  --output text)

aws lambda invoke --function-name "$FN" --region us-east-2 /dev/stdout | jq
aws logs tail "/aws/lambda/$FN" --since 10m --region us-east-2 | grep -A30 "DRY_RUN"
```

Check the feed list is right, the thresholds are right, and the message reads
the way you want it to in a busy channel.

### 4. Go live

```bash
./deploy.sh <cfn-artifact-bucket> s3-feed-freshness false
```

The PO prefix is currently well past its threshold, so the next invoke posts a
real alert immediately — free end-to-end validation.

### 5. Wire the health alarms

A monitor that dies silently is indistinguishable from a healthy feed.

```bash
ALARM_TOPIC_ARN=arn:aws:sns:us-east-2:057311931122:platform-alerts \
  ./deploy.sh <cfn-artifact-bucket> s3-feed-freshness false
```

---

## Common operations

**Add a vendor feed** — edit `FeedsJson` and redeploy. No code change.

```json
[
  {"id":"enterprise-cafe/purchase-orders","prefix":"enterprise-cafe/prod/incoming/purchase-orders/"},
  {"id":"birite/catalog","prefix":"birite/prod/incoming/catalog/","stale_days":7}
]
```

`suffix` and `stale_days` are optional per-feed overrides.

**Silence a noisy feed** — raise its `stale_days`, or drop it from `FeedsJson`.

**Reset alert state** — a feed stuck reporting STALE after you've fixed it:

```bash
aws ssm put-parameter --name /platform-monitors/s3-feed-freshness/watermark \
  --value '{}' --type String --overwrite --region us-east-2
```

**Stop re-notification** — set `RenotifyHours` to `0` for one alert per outage.

**Pause the monitor** — disable the EventBridge rule rather than deleting the
stack, so the watermark survives.

---

## Triage: an alert fired

1. Is the vendor actually down, or did the drop land somewhere else? Check the
   prefix directly:
   `aws s3 ls s3://cut-dry-vendor-integration/<prefix> --recursive | sort -k1,2 | tail -5`
2. Filenames carry their own date that runs 1–3 days behind upload time — a file
   named `20260818` may have arrived on the 21st. Trust `LastModified`.
3. If files are arriving but Snowflake is empty, this monitor is not your
   problem — check
   `SYSTEM$PIPE_STATUS('EXTERNAL_INTEGRATIONS.ENTERPRISE_CAFE.PURCHASE_ORDERS_PIPE')`.

## Triage: no alert, but the feed is dead

1. Is the schedule firing? `Invocations` on the function, or the
   `-not-running` alarm.
2. Is it still in dry-run? Check the `DryRunMode` stack output.
3. Is the watermark stuck at STALE with a recent `last_notified_at`, suppressing
   re-notification? Read the parameter.
4. Did the Slack post fail? A failure raises, so look for `Errors > 0` and read
   the log — the exception names the cause.

---

## Testing without AWS

```bash
python3 cfn/run_local.py --offline --scenario mixed --reset
python3 cfn/run_local.py --offline --scenario stale
python3 cfn/run_local.py --offline --scenario recovered
```

Runs the real handler with S3 and SSM stubbed. No credentials needed.

> **Never drop probe files into the prod prefix.** Anything matching
> `.*PO.*\.csv` is ingested by the live Snowpipe into the real `PURCHASE_ORDERS`
> table. Test recovery with the offline harness or a scratch prefix.

---

## IAM

The stack creates its own execution role. Six statements:

| Action | Resource |
|---|---|
| `s3:ListBucket` | the monitored bucket |
| `ssm:GetParameter`, `ssm:PutParameter` | the watermark parameter only |
| `ssm:GetParameter` | the webhook parameter only |
| `kms:Decrypt` | `*`, conditioned to `ssm.us-east-2` |
| `logs:CreateLogStream`, `logs:PutLogEvents` | its own log group |

### Deploy permissions

Two routes, and they need different things.

**Via the `aws-infra` pipeline** — if that pipeline's role already deploys the
RDS and EC2 monitors, it covers this stack too. No new permission request.

**Deploying by hand** — `deploy-policy.json` in this folder is the minimum
policy. 13 statements, every one scoped to `s3-feed-freshness*` in us-east-2,
`PassRole` conditioned to `lambda.amazonaws.com`. Administrator access is not
required. Replace `REPLACE-WITH-ARTIFACT-BUCKET` with the bucket used for
`aws cloudformation package` before attaching.

```bash
aws iam create-policy --policy-name s3-feed-freshness-deploy \
  --policy-document file://cfn/deploy-policy.json
aws iam attach-user-policy --user-name eniyavant \
  --policy-arn arn:aws:iam::057311931122:policy/s3-feed-freshness-deploy
```

Confirmed as of 09/03/26: user `eniyavant` has no CloudFormation access at all —
even `cloudformation:ValidateTemplate` is denied — so one of these two routes is
mandatory before any deploy step will work.
