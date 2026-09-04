# Runbook — S3 staleness monitor

Alerts **#slack-test** when `s3://cut-and-dry-test/test-dev/` has gone three
consecutive days without a new file.

| | |
|---|---|
| Account | 147723036280 — Cut+Dry Eng (non-prod) |
| Region | us-east-2 |
| Stack | `s3-staleness-monitor` |
| Function | `s3-staleness-monitor` |
| Log group | `/aws/lambda/s3-staleness-monitor` |
| Schedule | `cron(0 15 * * ? *)` — 08:00 PT daily |
| Channel | #slack-test |

Every command below assumes `--profile non-prod-sso --region us-east-2`. Export
it once and drop the flags:

```bash
export AWS_PROFILE=non-prod-sso
export AWS_DEFAULT_REGION=us-east-2
```

---

## Contents

1. [Prove it offline](#1-prove-it-offline)
2. [Sign in and check the target](#2-sign-in-and-check-the-target)
3. [Create the Slack webhook](#3-create-the-slack-webhook)
4. [Store the webhook in SSM](#4-store-the-webhook-in-ssm)
5. [Deploy in dry-run](#5-deploy-in-dry-run)
6. [Read the dry-run output](#6-read-the-dry-run-output)
7. [Go live](#7-go-live)
8. [Prove the alert for real](#8-prove-the-alert-for-real)
9. [Day-to-day operations](#day-to-day-operations)
10. [Triage](#triage)
11. [Teardown](#teardown)
12. [What is still unconfirmed](#what-is-still-unconfirmed)

---

## 1. Prove it offline

Costs nothing, needs no credentials, and catches most mistakes.

```bash
python3 run_local.py            # six scenarios against a fake S3
python3 -m unittest test_monitor -v
```

`run_local.py` prints the exact Slack message each scenario would produce.
Read one — the wording is what people will actually see at 08:00.

Expected: `all 6 scenarios behaved as expected`, and `Ran 30 tests … OK`.

Change the threshold or the message and re-run this before deploying anything.

---

## 2. Sign in and check the target

```bash
aws sso login --profile non-prod-sso
aws sts get-caller-identity
```

Account must read **147723036280**. If it does not, you are in the wrong
account and `deploy.sh` will refuse to run anyway.

Now confirm the bucket is real and readable from this account:

```bash
aws s3api head-bucket --bucket cut-and-dry-test
aws s3 ls s3://cut-and-dry-test/test-dev/ --summarize | tail -5
```

- **`404`/`NoSuchBucket`** — the bucket does not exist in this account. Check
  whether it actually lives in prod (057311931122); if it does, this monitor
  needs to be deployed there instead, not here.
- **`403`** — it exists but `NonProdDeveloper` cannot list it. A bucket policy
  is in the way. Fix that before deploying — the Lambda will hit the same wall.
- **An empty listing** — fine, and worth knowing now: an empty prefix reads as
  "no file has ever landed", which is past any threshold, so the very first live
  run will alert. That is correct behaviour, not a bug.

---

## 3. Create the Slack webhook

The monitor posts through an incoming webhook. One per channel.

1. Go to <https://api.slack.com/apps> and sign in to the Cut+Dry workspace.
2. **Create New App → From scratch.** Name it something recognisable in an
   audit later — `S3 Staleness Monitor` — and pick the Cut+Dry workspace.
3. **Incoming Webhooks → Activate Incoming Webhooks → On.**
4. **Add New Webhook to Workspace**, choose **#slack-test**, click **Allow**.
5. Copy the URL. It looks like
   `https://hooks.slack.com/services/<T-id>/<B-id>/<token>`.

   > Keep the angle-bracket form when writing this path down anywhere. A
   > realistic-looking dummy token has the same shape as a real one, and
   > GitHub push protection blocks the push on shape alone.

> The webhook URL is a credential — anyone holding it can post to the channel as
> this app. Do not paste it into Slack, a ticket, a commit or a `.env` file. It
> goes straight into SSM in the next step and is read from there.

If workspace policy requires app installs to be approved by an admin, the
**Allow** step will queue a request instead. Ping #platform.

---

## 4. Store the webhook in SSM

```bash
aws ssm put-parameter \
  --name /platform-monitors/s3-staleness/slack-test-webhook \
  --type SecureString \
  --description "Incoming webhook for #slack-test - S3 staleness monitor" \
  --value 'https://hooks.slack.com/services/<T-id>/<B-id>/<token>'
```

Note the single quotes, and the leading space before `aws` if your shell is
configured with `HISTCONTROL=ignorespace` — that keeps the URL out of
`~/.bash_history`.

Confirm it landed, without printing the value:

```bash
aws ssm describe-parameters \
  --parameter-filters "Key=Name,Values=/platform-monitors/s3-staleness/slack-test-webhook" \
  --query 'Parameters[0].[Name,Type,LastModifiedDate]' --output table
```

Type must be **SecureString**. `deploy.sh` checks this and refuses a plaintext
`String`.

Optional — prove the webhook works before any AWS is involved:

```bash
curl -X POST -H 'Content-Type: application/json' \
  -d '{"text":"S3 staleness monitor - webhook test, please ignore"}' \
  'https://hooks.slack.com/services/...'
```

Expect `ok`, and a message in #slack-test.

---

## 5. Deploy in dry-run

```bash
./deploy.sh nonprod
```

`DryRun=true` is the default, so forgetting the argument gives you the harmless
mode. The script, in order:

1. refuses to continue unless your credentials are for 147723036280;
2. refuses unless the webhook parameter exists and is a SecureString;
3. creates the artifact bucket if it is missing (private, encrypted);
4. packages `src/` and deploys the stack.

**If step 4 fails with `AccessDenied` on `iam:CreateRole`:** `NonProdDeveloper`
may not be allowed to create roles. Re-run the first deploy with the break-glass
role, which has a 1-hour session:

This is not hypothetical - it is what happens on a first deploy. The exact
error is a 403 on `iam:CreateRole`, wrapped in a confusing
`UnauthorizedTaggingOperation` message; ignore the tagging part and read the
inner reason.

Add a `non-prod-admin` profile if you do not have one. It reuses the existing
`sso-session`, so only the profile block is new:

```bash
printf '\n[profile non-prod-admin]\nsso_session = non-prod-sso\nsso_account_id = 147723036280\nsso_role_name = NonProdAdmin\nregion = us-east-2\noutput = json\n' >> ~/.aws/config
aws configure list-profiles
```

Check `list-profiles` succeeds before going on - a malformed `~/.aws/config`
breaks every AWS command, not just this one.

A failed CREATE leaves the stack in `ROLLBACK_COMPLETE`, which cannot be
updated. Delete it, then deploy under the admin role:

```bash
aws cloudformation delete-stack --stack-name s3-staleness-monitor
aws cloudformation wait stack-delete-complete --stack-name s3-staleness-monitor

aws sso login --profile non-prod-admin
PROFILE=non-prod-admin ./deploy.sh nonprod
```

`PROFILE=` in front of the command, not `AWS_PROFILE=` - `deploy.sh` sources
`PROFILE` from `env/<name>.env` and an override has to use the same name to
win. The account guard still applies, so this cannot land in the wrong account.

Once the role exists, every later deploy works under `NonProdDeveloper` again.
The NonProdAdmin session is only 1 hour, so do not leave it half-finished.

**The better long-term fix** is a scoped deploy policy attached to
`NonProdDeveloper` rather than break-glass every time the role changes. The
previous implementation in this repo had one - recover it with
`git show 08593eb:cfn/deploy-policy.json` - and ask #platform to attach it.

---

## 6. Read the dry-run output

Run it on demand rather than waiting for 08:00:

```bash
aws lambda invoke --function-name s3-staleness-monitor \
  /dev/stdout | python3 -m json.tool
```

You are looking for four things:

```jsonc
{
  "dry_run": true,
  "webhook_check": { "ok": true, "detail": "hooks.slack.com" },  // ← the webhook resolves
  "prefixes_checked": 1,
  "prefixes_stale": 0,                                            // ← or 1
  "slack_posts": [],
  "feeds": [
    {
      "feed_id": "test-dev",
      "objects": 42,
      "last_file_at": "2026-09-03T14:22:11+00:00",
      "elapsed_days": 0.91,
      "threshold_days": 3,
      "status": "OK"
    }
  ]
}
```

- **`webhook_check.ok` must be `true`.** If it is `false`, read `detail`: a
  wrong parameter path, a missing `ssm:GetParameter` grant, or a value that is
  not a Slack webhook. Fix it before going live — this is the one thing a clean
  dry run would otherwise not prove.
- **`objects`** should match what `aws s3 ls` showed. A `0` here against a
  non-empty prefix usually means `ObjectSuffix` is filtering everything out.
- **`elapsed_days`** should look plausible against the newest file you can see.

If `prefixes_stale` is `1`, the log holds the exact message it would have sent:

```bash
aws logs tail /aws/lambda/s3-staleness-monitor --since 15m --format short
```

Read the wording. This is the last cheap moment to change it.

---

## 7. Go live

```bash
./deploy.sh nonprod false
```

Only `DryRun` changes. Invoke once to confirm:

```bash
aws lambda invoke --function-name s3-staleness-monitor \
  /dev/stdout | python3 -m json.tool
```

If the prefix is currently stale, a message appears in #slack-test within a
second or two. If it is fresh, nothing happens — which is the design, and the
reason for step 8.

Back to dry-run at any time: `./deploy.sh nonprod`.

---

## 8. Prove the alert for real

A monitor nobody has seen fire is a monitor nobody should trust. Do not wait
three days, and **do not put probe files into the prefix** — the bucket has
`trigger_sync` wired to it and anything you drop there gets processed.

Temporarily lower the threshold instead, so the current contents read as stale:

Lowering `StaleDays` only works when the newest file is already older than the
floor of 1 day. When the prefix has a file from today it cannot fire, so make
the monitor look at something that genuinely has nothing in it instead. Setting
a suffix that matches no object is the cleanest way - it touches no data, and
the prefix, the bucket and the schedule all stay exactly as they are:

```bash
sed -i.bak 's/^OBJECT_SUFFIX=$/OBJECT_SUFFIX=.no-such-suffix/' env/nonprod.env
./deploy.sh nonprod false
aws lambda invoke --function-name s3-staleness-monitor /dev/stdout | python3 -m json.tool
```

Zero objects match, which reads as "no file has ever landed here" - past any
threshold - so it alerts. That exercises the entire delivery path: schedule,
IAM role, SSM lookup, webhook, and the message itself.

Put it back and confirm the next run is silent again:

```bash
mv env/nonprod.env.bak env/nonprod.env
./deploy.sh nonprod false
aws lambda invoke --function-name s3-staleness-monitor /dev/stdout | python3 -m json.tool
```

The `feeds[0].objects` count should return to its real value and `status` to
`OK`. If it does not, the suffix did not get reset - check `env/nonprod.env`.

Check the message renders properly in Slack — the bold prefix name, the
backticked path, the timestamp in the right timezone. Then put the threshold
back and confirm the next invoke is silent.

---

## Day-to-day operations

**Run it now**

```bash
aws lambda invoke --function-name s3-staleness-monitor /dev/stdout | python3 -m json.tool
```

**See the last week of runs**

```bash
aws logs tail /aws/lambda/s3-staleness-monitor --since 7d --format short \
  --filter-pattern '{ $.prefixes_stale = * }'
```

**Watch a second prefix** — edit `FEEDS_JSON` in `env/nonprod.env`:

```bash
FEEDS_JSON='[{"id":"test-dev","prefix":"test-dev/"},{"id":"test-uat","prefix":"test-uat/","stale_days":5}]'
```

then `./deploy.sh nonprod false`. Per-prefix `stale_days` and `suffix` both
override the defaults. One aggregated Slack message covers every stale prefix.

**Change the threshold or the schedule** — `STALE_DAYS` / `SCHEDULE_EXPRESSION`
in the same file, then redeploy. Cron is UTC: `cron(0 15 * * ? *)` is 08:00 PT
during daylight saving, 07:00 PT outside it.

**Silence it without deleting anything**

```bash
aws events disable-rule --name s3-staleness-monitor-schedule
# and back:
aws events enable-rule --name s3-staleness-monitor-schedule
```

Or `./deploy.sh nonprod` to put it back in dry-run — it keeps running and
logging, just stops posting.

---

## Triage

### An alert fired

1. Confirm it independently:

   ```bash
   aws s3 ls s3://cut-and-dry-test/test-dev/ --recursive | sort -k1,2 | tail -5
   ```

   The last line is the newest object. If that is genuinely old, the alert is
   correct and the question moves upstream to whatever writes into the prefix.

2. If the listing shows a recent file the monitor did not see, check
   `ObjectSuffix` and the object's size — zero-byte objects and keys ending in
   `/` are skipped by design.

3. Remember `LastModified` is upload time. A file named for last week that was
   uploaded this morning counts as fresh.

### It alerts every day for the same outage

Expected. The monitor is stateless, so it re-reports a stale prefix on every
run. Either fix the feed or disable the rule while it is being worked on. If the
repetition is genuinely a problem, see "Stateless, deliberately" in `README.md`
for the ~30-line fix.

### Nothing has been posted for days — is it healthy or dead?

That is the ambiguity the health alarm exists to resolve, and `AlarmTopicArn` is
currently empty. Until an SNS topic is wired in, check by hand:

```bash
aws logs tail /aws/lambda/s3-staleness-monitor --since 2d --format short | tail -20
aws cloudwatch get-metric-statistics --namespace AWS/Lambda \
  --metric-name Invocations --dimensions Name=FunctionName,Value=s3-staleness-monitor \
  --start-time "$(date -u -v-7d +%Y-%m-%dT%H:%M:%S 2>/dev/null || date -u -d '7 days ago' +%Y-%m-%dT%H:%M:%S)" \
  --end-time "$(date -u +%Y-%m-%dT%H:%M:%S)" --period 86400 --statistics Sum \
  --output table
```

One invocation per day means it is running and the feed is fine.

### The Lambda is erroring

```bash
aws logs tail /aws/lambda/s3-staleness-monitor --since 1d --filter-pattern 'ERROR'
```

| Message | Cause | Fix |
|---|---|---|
| `AccessDenied` on `ListObjectsV2` | Bucket policy or wrong bucket name | Check `head-bucket` from step 2 |
| `ParameterNotFound` | Webhook parameter missing or renamed | Step 4 |
| `AccessDeniedException` on `ssm:GetParameter` | Parameter path changed but the stack was not redeployed | Redeploy — the IAM policy is scoped to the exact path |
| `is not a Slack webhook URL` | Wrong value in the parameter | Step 4 |
| `Slack webhook returned 404` | Webhook revoked, or the app was removed from the channel | Recreate it, step 3 |
| `Slack webhook returned 403` | Channel archived or app uninstalled | Check #slack-test still exists |

Errors are intentionally not swallowed, so each of these shows up as a Lambda
error rather than as silence.

---

## Teardown

```bash
aws cloudformation delete-stack --stack-name s3-staleness-monitor
aws cloudformation wait stack-delete-complete --stack-name s3-staleness-monitor
```

Removes the function, role, schedule, log group and alarm. Not removed, on
purpose:

```bash
# the webhook secret
aws ssm delete-parameter --name /platform-monitors/s3-staleness/slack-test-webhook
# the artifact bucket
aws s3 rb s3://s3-staleness-monitor-artifacts-147723036280 --force
```

The monitored bucket is never touched by any of this.

---

## What is still unconfirmed

Three assumptions in this repo that nothing has verified. None blocks the code;
the first two decide whether the first live run reaches anybody.

**1. Whether `cut-and-dry-test` is in this account.** The name looks non-prod
and the request pointed at the Eng account, but nothing has confirmed the bucket
is in 147723036280 rather than prod. Step 2 settles it in one command. If it
turns out to be in prod, the stack goes to 057311931122 instead and the deploy
policy question in point 3 becomes live.

**2. The webhook does not exist yet.** `/platform-monitors/s3-staleness/slack-test-webhook`
is the path this repo will use, not a path anyone has confirmed. Steps 3 and 4
create it. There is no existing "platform webhook" for #slack-test to reuse —
the other platform monitors post to #platform-team.

**3. Nothing watches the monitor.** `AlarmTopicArn` is empty because no SNS
topic has been confirmed in this account, so a crashed monitor is currently
indistinguishable from a healthy feed. Find or create a topic and set it:

```bash
aws sns list-topics --query 'Topics[].TopicArn' --output table
```

This is the gap most worth closing before anyone relies on the alert.
