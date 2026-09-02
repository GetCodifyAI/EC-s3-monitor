# S3 feed staleness monitor → Slack

Alerts a Slack channel when no new file has landed in
`s3://cut-dry-vendor-integration/enterprise-cafe/prod/incoming/purchase-orders/`
for three consecutive days.

```
EventBridge (daily cron)
        │
        ▼
  Lambda: s3-feed-monitor
        ├── ListObjectsV2 on the prefix → newest LastModified
        ├── DynamoDB item → previous OK/STALE status, last notification time
        ├── CloudWatch → HoursSinceLastFile, ObjectsInPrefix
        └── Slack incoming webhook (only on transition / re-notify interval)
```

Nothing about the existing pipeline changes. The monitor only reads.

## Why polling instead of S3 events

The `incoming/purchase-orders/` prefix already has an `s3:ObjectCreated:*`
notification wired to
`arn:aws:sns:us-east-2:057311931122:ingest-enterprise-cafe-data-to-snowflake`
for Snowpipe auto-ingest. S3 rejects a second notification config whose
prefix/suffix overlaps an existing one for the same event type, so adding a
direct S3 → Lambda trigger here would fail or require rewriting the config
Snowpipe depends on.

If you later want event-driven freshness (large prefixes, sub-hour detection),
subscribe a small Lambda to that **existing** SNS topic and have it stamp a
watermark into the same DynamoDB item. Snowpipe is unaffected by extra
subscribers. Until the prefix holds tens of thousands of objects, the daily
list scan is cheaper to build and operate.

## Deploy

```bash
cd terraform
terraform init
terraform apply

# put the webhook in the empty secret (never in tfvars or state)
aws secretsmanager put-secret-value \
  --secret-id "$(terraform output -raw slack_secret_arn)" \
  --secret-string '{"webhook_url":"https://hooks.slack.com/services/T000/B000/xxxx"}' \
  --region us-east-2

# get notified when the monitor itself dies
aws sns subscribe \
  --topic-arn "$(terraform output -raw health_topic_arn)" \
  --protocol email --notification-endpoint data-alerts@cutanddry.com \
  --region us-east-2
```

The webhook comes from a Slack app → **Incoming Webhooks** → *Add New Webhook
to Workspace*, scoped to the target channel. One webhook posts to exactly one
channel.

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `stale_days` | `3` | Threshold in days |
| `business_days_only` | `false` | Count Mon–Fri only. Set to `true` if the vendor never drops on weekends — otherwise a Friday delivery alerts on Monday. |
| `suffix` | `.csv` | Only `.csv` objects count as a delivery, so a stray `.tmp` or `_SUCCESS` marker won't reset the clock |
| `renotify_hours` | `24` | Re-ping while stale. `0` = one alert per outage. |
| `schedule_expression` | `cron(0 15 * * ? *)` | 08:00 PT |
| `display_tz` | `America/Los_Angeles` | Times in the Slack message render as `MM/DD/YY HH:MM` |

## Test

```bash
FN=$(cd terraform && terraform output -raw function_name)

# 1. real run
aws lambda invoke --function-name "$FN" --region us-east-2 /dev/stdout | jq

# 2. force an alert without waiting three days: temporarily set STALE_DAYS=0,
#    invoke, confirm the Slack message, then set it back to 3
aws lambda update-function-configuration --function-name "$FN" \
  --region us-east-2 \
  --environment "Variables={$(aws lambda get-function-configuration \
      --function-name "$FN" --region us-east-2 \
      --query 'Environment.Variables' --output json \
    | jq -r 'to_entries|map("\(.key)=\(.value)")|join(",")' \
    | sed 's/STALE_DAYS=[0-9]*/STALE_DAYS=0/')}"
```

Set `DRY_RUN=true` to log the Slack payload instead of posting it.

> **This is the production prefix.** Anything matching `.*PO.*\.csv` dropped
> here will be ingested by the Snowpipe into the real `PURCHASE_ORDERS` table.
> Do not drop probe files here. Instead, point `PREFIX` at a scratch prefix for
> the recovery test, or verify recovery by waiting for a genuine vendor drop.

## Verification checklist

- If the prefix is already past the threshold, the first real invoke posts a
  `:rotating_light:` message immediately — a free way to validate the Slack path.
- Second invoke within `renotify_hours` → no duplicate post,
  `slack_notified: false` in the response.
- New file arrives → next invoke posts the recovery message and flips the
  DynamoDB item to `OK`.
- Empty prefix → alerts with "no files ever" rather than throwing.
- Delete the EventBridge rule → `-not-running` alarm fires within two days.

## Known limitations

- **Detects absence, not correctness.** A vendor dropping a 0-byte or truncated
  file resets the clock. Zero-byte objects are already skipped; row-count
  validation belongs in the dedup Lambda, not here.
- **Detects S3 arrival, not Snowflake load.** A file landing while Snowpipe is
  broken looks healthy to this monitor. Pair it with a check on
  `SYSTEM$PIPE_STATUS('EXTERNAL_INTEGRATIONS.ENTERPRISE_CAFE.PURCHASE_ORDERS_PIPE')`
  or `COPY_HISTORY` if you want end-to-end coverage.
- **`LastModified` is arrival time, not business date.** A backfill of old POs
  today counts as fresh.
- **Same-day granularity.** A daily cron means worst-case detection latency of
  ~24h past the threshold. Run twice a day if that matters.
- `zoneinfo` needs the tz database in the runtime image. If the log shows
  `Timezone ... unavailable`, add the `tzdata` package to the zip; times fall
  back to UTC in the meantime rather than failing.
