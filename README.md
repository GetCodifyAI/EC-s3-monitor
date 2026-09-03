# S3 feed freshness monitor

Alerts a Slack channel when a vendor feed prefix in
`s3://cut-dry-vendor-integration/` has received no new file for N consecutive
business days. Silent on a clean run.

```
EventBridge (daily cron)
        │
        ▼
  Lambda: s3-feed-freshness
        ├── ListObjectsV2 per feed → newest LastModified
        ├── SSM watermark → previous OK/STALE per feed
        └── Slack (platform incoming webhook, plain text)
            one aggregated post, on transition only
```

Built to the same pattern as the other platform alerts — the RDS
zero-connections and EC2 over-provisioned monitors — so it ships as a
CloudFormation template plus a runbook, deploys in dry-run first, and is
intended to live in `aws-infra`.

## Layout

| Path | What |
|---|---|
| `cfn/template.yaml` | SAM/CloudFormation stack: role, Lambda, log group, schedule, watermark parameter, optional alarms |
| `cfn/src/monitor.py` | The handler |
| `cfn/RUNBOOK.md` | **Start here.** Deployment, triage in both directions, common operations |
| `cfn/deploy.sh` | Package and deploy, dry-run by default |
| `cfn/run_local.py` | Offline harness — runs the real handler with no AWS credentials |

`cfn/` is deliberately self-contained so it can be copied into `aws-infra` as a
single folder.

## Quick start

```bash
# prove the logic with zero AWS access
python3 cfn/run_local.py --offline --scenario mixed --reset

# deploy in dry-run: logs the Slack payload, posts nothing
cd cfn && ./deploy.sh <cfn-artifact-bucket>

# once the logged output looks right
./deploy.sh <cfn-artifact-bucket> s3-feed-freshness false
```

Full detail in [`cfn/RUNBOOK.md`](cfn/RUNBOOK.md).

## Configuration

Everything is a CloudFormation parameter — adding a vendor feed is a parameter
change, not a code change.

| Parameter | Default | Notes |
|---|---|---|
| `FeedsJson` | the PO feed | Array of `{id, prefix}`, with optional per-feed `suffix` and `stale_days` |
| `StaleDays` | `3` | Default threshold |
| `BusinessDaysOnly` | `true` | Count Mon–Fri only, so a Friday drop does not alert on Monday |
| `ObjectSuffix` | `.csv` | A stray `.tmp` or `_SUCCESS` marker won't reset the clock |
| `SlackWebhookParam` | `/platform/slack/webhook-dam-alerts` | SSM SecureString holding the platform webhook |
| `RenotifyHours` | `24` | Re-ping while stale. `0` = one alert per outage |
| `DryRun` | `true` | Logs the payload instead of posting, and skips the watermark write |
| `AlarmTopicArn` | *(empty)* | Existing SNS topic for the health alarms. Empty = no alarms created |
| `ScheduleExpression` | `cron(0 15 * * ? *)` | 08:00 PT |

## Design notes

**Polling, not S3 events.** The prefix already carries an `s3:ObjectCreated:*`
notification pointed at the Snowpipe SNS topic
(`ingest-enterprise-cafe-data-to-snowflake`). S3 rejects a second notification
config whose prefix overlaps an existing one for the same event type, so a
direct S3 → Lambda trigger would fail or force a rewrite of the config Snowpipe
depends on. The bucket's notification config is not touched. If sub-hour
detection is ever needed, subscribe to that existing SNS topic instead —
Snowpipe is unaffected by extra subscribers.

**The watermark is in SSM, never S3.** One small parameter holds
`{feed_id: {status, last_notified_at}}`. Writing a state object into
`cut-dry-vendor-integration` would fire that bucket's `ObjectCreated`
notification into the Snowpipe SNS topic. Do not "simplify" this to an S3
object later.

**Not fully stateless.** The watermark costs one parameter and no extra service,
and buys transition-only alerting plus a recovery message. Without it a stale
feed alerts every day of an outage and recovery is never reported.

**A failed Slack post raises.** That increments the Lambda `Errors` metric and
trips the alarm. A monitor that swallows its own delivery failures looks exactly
like a healthy feed.

**No `s3:GetObject`.** Listing metadata only — the monitor cannot read a row of
vendor data.

## Known limits

- **Detects absence, not correctness.** A truncated file resets the clock.
  Zero-byte objects are skipped; row-count validation belongs elsewhere.
- **Detects S3 arrival, not Snowflake load.** A file landing while Snowpipe is
  broken looks healthy here. Pair with a check on
  `SYSTEM$PIPE_STATUS('EXTERNAL_INTEGRATIONS.ENTERPRISE_CAFE.PURCHASE_ORDERS_PIPE')`
  for end-to-end coverage.
- **`LastModified` is upload time, not business date.** PO filenames carry their
  own date running 1–3 days behind. A backfill of old POs today counts as fresh.
- **Same-day granularity.** Worst-case detection latency is about 24 hours past
  the threshold.

> **Never drop probe files into the prod prefix.** Anything matching
> `.*PO.*\.csv` is ingested by the live Snowpipe into the real `PURCHASE_ORDERS`
> table. Use the offline harness or a scratch prefix.
