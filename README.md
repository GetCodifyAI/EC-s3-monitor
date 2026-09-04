# S3 staleness monitor

Alerts **#slack-test** when `s3://cut-and-dry-test/test-dev/` has received no new
file for three consecutive days. Silent on a clean run.

```
EventBridge (daily, 08:00 PT)
        │
        ▼
  Lambda: s3-staleness-monitor
        ├── ListObjectsV2 on test-dev/  →  newest LastModified
        ├── idle >= 3 days?  →  build the message
        └── Slack incoming webhook (plain text, one aggregated post)
```

Account **147723036280** (Cut+Dry Eng), region **us-east-2**. Deployed as a
CloudFormation stack, dry-run first.

## Layout

| Path | What |
|---|---|
| `RUNBOOK.md` | **Start here.** Setup, deploy, triage, teardown |
| `src/monitor.py` | The handler |
| `template.yaml` | CloudFormation: role, Lambda, log group, schedule, optional alarm |
| `deploy.sh` | Package and deploy. Dry-run by default, guards the target account |
| `env/nonprod.env` | Account, bucket, prefixes, threshold, schedule |
| `run_local.py` | Offline harness — runs the real handler with no AWS credentials |
| `test_monitor.py` | 30 unit tests, no AWS and no network |
| `stubs.py` | Fake boto3 shared by the harness and the tests |

## Quick start

```bash
# 1. prove the logic with zero AWS access
python3 run_local.py
python3 -m unittest test_monitor

# 2. deploy to non-prod in dry-run
aws sso login --profile non-prod-sso
./deploy.sh nonprod

# 3. read what it would have posted, then go live
./deploy.sh nonprod false
```

Full detail, including creating the Slack webhook, in [`RUNBOOK.md`](RUNBOOK.md).

## Configuration

Everything is a CloudFormation parameter, set from `env/nonprod.env`. Watching a
second prefix, changing the threshold or moving the channel is a config change
and a redeploy — never a code change.

| Parameter | Default | Notes |
|---|---|---|
| `MonitoredBucket` | `cut-and-dry-test` | |
| `FeedsJson` | `[{"id":"test-dev","prefix":"test-dev/"}]` | Array of `{id, prefix}`, optional `suffix` and `stale_days` per entry |
| `StaleDays` | `3` | Alerts at `>=`, so exactly 3 days alerts |
| `BusinessDaysOnly` | `false` | `true` counts Mon–Fri only |
| `ObjectSuffix` | *(empty)* | e.g. `.csv` to ignore stray `.tmp` files |
| `SlackWebhookParam` | `/platform-monitors/s3-staleness/slack-test-webhook` | SSM SecureString |
| `DryRun` | `true` | Logs the message instead of posting it |
| `ScheduleExpression` | `cron(0 15 * * ? *)` | 08:00 PT daily, UTC cron |
| `DisplayTimeZone` | `America/Los_Angeles` | Timestamps in the message |
| `LogRetentionDays` | `30` | |
| `AlarmTopicArn` | *(empty)* | SNS topic for the monitor's own health alarm |

## Design notes

**Polling, not S3 events.** The bucket already has an EventBridge configuration
and a `trigger_sync` Lambda wired to it. S3 rejects a second notification config
whose prefix overlaps an existing one for the same event type, so adding a
direct trigger risks breaking what is already there. Nothing about the bucket is
touched. If sub-hour detection is ever needed, add a rule to the existing
EventBridge config rather than a new notification.

**Stateless, deliberately.** There is no watermark, no DynamoDB table and no
state object. Every run recomputes the answer from S3 alone. The cost is that a
stale prefix alerts once per run for as long as it stays stale — one message a
day on the default schedule, which reads as a standing reminder rather than a
storm. The benefit is that there is nothing to get out of sync, nothing to
migrate, and nothing to clear when someone wants to re-test. If the daily repeat
does turn into noise, the fix is a small SSM parameter holding
`{prefix: last_notified_at}` — about 30 lines — not a database.

**Nothing is ever written to the monitored bucket.** Not a state file, not a
probe object. The IAM role grants `s3:ListBucket` and nothing else: no
`PutObject`, and no `GetObject` either, so the monitor cannot read a byte of
whatever lands there.

**The dry run checks the webhook.** Resolving the webhook parameter is the one
step a clean dry run would otherwise skip, and it is the assumption most likely
to be wrong — a mistyped path, a missing grant, a parameter holding something
that is not a webhook. Every dry run resolves it and reports `webhook_check`.
The host is logged; the URL never is, because the URL is itself the credential.

**A failed Slack post raises.** That increments the Lambda `Errors` metric. Set
`AlarmTopicArn` and it trips an alarm. A monitor that swallows its own delivery
failures looks exactly like a healthy feed.

**Zero-byte objects and folder keys do not reset the clock.** A `_SUCCESS`
marker or a console-created folder placeholder landing in the prefix is not a
delivery.

## Known limits

- **Detects absence, not correctness.** A truncated or malformed file resets the
  clock. Content validation belongs elsewhere.
- **`LastModified` is upload time, not business date.** A backfill of old files
  today counts as fresh.
- **Same-day granularity.** On a daily schedule, worst-case detection latency is
  about 24 hours past the threshold.
- **Silence is ambiguous without the alarm.** A healthy feed and a monitor that
  has stopped running look identical from the channel. `AlarmTopicArn` is what
  distinguishes them, and it is empty until an SNS topic is confirmed.
