# Production handover — S3 staleness monitor

**For** whoever deploys this in 057311931122 · **From** Eniyavan ·
**Repo** GetCodifyAI/EC-s3-monitor · **Date** 09/08/26

## What it does

A Lambda runs once a day and lists
`s3://cut-dry-vendor-integration/enterprise-cafe/prod/incoming/purchase-orders/`.
If the newest `.csv` there is three or more **business days** old, it posts one
plain-text message to Slack. On a healthy day it does nothing at all — no
message, no noise.

It is already running in non-prod (147723036280) against a test prefix, live
since 09/08/26, and has fired a real alert end to end. This is the same code
with a different `env/*.env` file.

## Read this before anything else

**Do not drop probe files into the monitored prefix.** Per the earlier
implementation of this monitor, that prefix feeds a live Snowpipe
(`ingest-enterprise-cafe-data-to-snowflake`) which ingests anything matching
`.*PO.*\.csv` straight into the real `PURCHASE_ORDERS` table. I have not
re-verified that myself — it is carried forward from earlier work in this repo
— but treat it as true until someone confirms otherwise. To test, use the
offline harness or lower the threshold; never write to the bucket.

**Nothing here touches the bucket's configuration.** No notification config is
added or changed. That is deliberate: S3 rejects a second notification config
whose prefix overlaps an existing one for the same event type, so adding a
direct S3 → Lambda trigger risks breaking the Snowpipe wiring. That is why this
polls on a schedule instead of subscribing to events. Please keep it that way.
If sub-hour detection is ever wanted, subscribe to the existing SNS topic —
Snowpipe is unaffected by extra subscribers.

**The monitor cannot read vendor data.** Its IAM role grants `s3:ListBucket` and
nothing else against that bucket — no `GetObject`, no `PutObject`. It sees
object names, sizes and timestamps; it cannot read a row, and cannot write into
the prefix it watches.

## Two prerequisites in 057311931122

Neither exists in the prod account yet. `deploy.sh` checks the first and refuses
to run without it.

### 1. A Slack webhook, stored as a SecureString

The non-prod webhook lives in 147723036280 and is not visible from prod, so
this account needs its own.

Create the incoming webhook at <https://api.slack.com/apps> (Incoming Webhooks →
Add New Webhook to Workspace → pick the channel), then:

```bash
 aws ssm put-parameter --name /platform-monitors/s3-staleness/dam-alerts-webhook --type SecureString --description "Incoming webhook for the S3 staleness monitor" --value 'PASTE_URL_HERE'
```

Leading space keeps it out of shell history. The URL is a bearer token for
posting to that channel — it should not appear in a ticket, a commit, or Slack
itself.

> **On the channel.** `env/prod.env` points at #dam-alerts because that is what
> was asked for. Worth knowing before go-live: on 09/08/26 that channel was
> carrying about 12 DAM bot messages every 6 minutes — roughly 2,800 a day, all
> machine STARTED/COMPLETED lines, no human replies. An alert posted there
> scrolls off in well under a minute. This monitor is designed so that its
> silence is meaningful; in that channel its *noise* is not meaningful either.
> If there is a better home — #platform-team, where the other platform monitors
> post, or a dedicated channel — it is one parameter and one webhook to change.

### 2. An SNS topic for the monitor's own health alarm

`ALARM_TOPIC_ARN` is empty, which means **nothing watches the monitor**. Its
healthy state is silence, so a crashed Lambda and a working feed look identical
from Slack. In non-prod that is a known gap; in production it undermines the
whole thing.

```bash
aws sns list-topics --query 'Topics[].TopicArn' --output table
```

Put an existing topic ARN into `ALARM_TOPIC_ARN` in `env/prod.env` and the stack
creates a CloudWatch alarm on the Lambda's `Errors` metric. A failed Slack post
raises rather than being swallowed, so delivery failures trip it too.

## Deploying

```bash
git clone https://github.com/GetCodifyAI/EC-s3-monitor.git
cd EC-s3-monitor
```

Prove the logic first — no AWS, no network, about five seconds:

```bash
python3 run_local.py
python3 -m unittest test_monitor
```

Then, with credentials for 057311931122:

```bash
./deploy.sh prod
```

`DryRun` defaults to `true`. The Lambda logs the exact message it would post and
sends nothing. `deploy.sh` refuses to run unless `sts get-caller-identity`
returns 057311931122, so it cannot land in the wrong account.

Read what it would have sent:

```bash
aws lambda invoke --function-name s3-staleness-monitor /tmp/monitor-out.json > /dev/null && python3 -m json.tool /tmp/monitor-out.json
aws logs tail /aws/lambda/s3-staleness-monitor --since 10m --format short
```

`webhook_check.ok` must be `true` — that is the Lambda proving it can read the
secret out of SSM, and it is the one step a clean dry run would otherwise skip.
Check `objects` looks sane against `aws s3 ls` on the prefix, and that
`elapsed_days` is plausible.

Then:

```bash
./deploy.sh prod false
```

Back to dry-run at any time with `./deploy.sh prod`. To stop it without
deleting anything:
`aws events disable-rule --name s3-staleness-monitor-schedule`.

## What the deploy needs

Creating the stack needs `iam:CreateRole` — in non-prod, the standard developer
permission set did not have it, which is why this is being handed over rather
than deployed by me.

`deploy-policy.json` in this repo is a scoped policy covering exactly what the
deploy touches — 13 statements, name-scoped to `s3-staleness-monitor*`, with
`iam:PassRole` conditioned to Lambda and no `iam:AttachRolePolicy`, so it is not
a privilege-escalation path. **It is written for account 147723036280.** Using
it in prod means changing the account ID in every ARN. It is offered as
documentation of the blast radius, not as something to apply as-is.

## Behaviour worth knowing

**It is stateless.** No watermark, no table. Every run recomputes from S3. The
cost is that a stale feed re-alerts once per run — one message a day — rather
than once per outage. That was a deliberate choice; the fix, if the repetition
becomes a problem, is a small SSM parameter holding `{prefix: last_notified_at}`
and is about 30 lines. There is a working implementation of exactly that in this
repo's history: `git show 08593eb:cfn/src/monitor.py`.

**It detects absence, not correctness.** A truncated or malformed file resets
the clock. And `LastModified` is upload time, not business date — a backfill of
old POs today reads as fresh.

**It detects S3 arrival, not Snowflake load.** A file landing while Snowpipe is
broken looks healthy here. For end-to-end coverage, pair it with a check on
`SYSTEM$PIPE_STATUS('EXTERNAL_INTEGRATIONS.ENTERPRISE_CAFE.PURCHASE_ORDERS_PIPE')`.

## Configuration

Everything is a CloudFormation parameter, set from `env/prod.env`. Adding a
prefix, changing the threshold or moving the channel is a config change and a
redeploy — never a code change.

Full detail, triage tables and teardown: `RUNBOOK.md`.
