# Runbook — S3 feed freshness monitor

**Region** us-east-2 · **Owner** data-eng

| Environment | Account | Stack | Watches | Alerts to |
|---|---|---|---|---|
| `prod` | 057311931122 | `s3-feed-freshness` | `cut-dry-vendor-integration` | #dam-alerts |
| `nonprod` | 147723036280 | `s3-feed-freshness-nonprod` | a scratch bucket this repo creates | #slack-test |

Each environment is a file in `env/`, and `deploy.sh` reads the account out of it
and refuses to run when your credentials point somewhere else.

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

### 1. Confirm the webhook parameter exists

The Lambda reads the platform webhook from an SSM SecureString.
`/platform/slack/webhook-dam-alerts` is this stack's **default, not a verified
fact** — see [Open questions](#open-questions). Confirm the real path by name:

```bash
aws ssm describe-parameters --region us-east-2 \
  --query 'Parameters[?starts_with(Name, `/platform/slack/`)].[Name,Type]' \
  --output table
```

`describe-parameters` returns names and types, never values, and is the only SSM
read the deploy policy grants against this path — deliberately, so deploying
the monitor never requires a human to read the webhook secret. Proving the value
is *readable and well-formed* is the dry run's job (step 3).

If the platform webhook lives under a different path, pass
`SLACK_WEBHOOK_PARAM=/your/path` to `deploy.sh`.

### 2. Deploy in dry-run

```bash
cd cfn
./deploy.sh prod
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

Then check `webhook_check` in the returned JSON:

```json
"webhook_check": {"parameter": "/platform/slack/webhook-dam-alerts",
                  "ok": true, "detail": "hooks.slack.com"}
```

`ok: false` means the parameter path is wrong, the runtime role cannot read it,
or the value is not a Slack webhook URL — all of which would otherwise stay
hidden until the moment you flipped `DryRun` to `false` and the first real alert
was due. The check runs on every dry run, including a clean one where no feed is
stale, and it logs only the host, never the URL.

### 4. Go live

```bash
./deploy.sh prod false
```

The PO prefix is currently well past its threshold, so the next invoke posts a
real alert immediately — free end-to-end validation.

### 5. Wire the health alarms

A monitor that dies silently is indistinguishable from a healthy feed.

Set `ALARM_TOPIC_ARN` in `env/prod.env` and redeploy:

```bash
./deploy.sh prod false
```

---

---

## Non-prod: proving it end to end

Account 147723036280 (Cut+Dry Eng), us-east-2, alerting to **#slack-test**. This
is where you prove the monitor works before asking anyone to approve a prod
deploy — everything except the prod feed itself is real here: the schedule, the
IAM role, the SSM watermark, the business-day maths, the message text, and an
actual Slack post.

### Why non-prod cannot watch the real feed

`cut-dry-vendor-integration` lives in the **prod** account. A Lambda in non-prod
cannot list it without a bucket policy change on the prod side, which is exactly
the access nobody has yet. So non-prod watches a scratch bucket this repo
creates, `s3-feed-freshness-testfeed-147723036280`, and the prod feed stays
untouched.

That costs nothing in fidelity for the thing being tested. The monitor's only
input from S3 is "what is the newest object under this prefix, and when did it
land" — a scratch prefix answers that exactly the way the real one does.

### Sign in

Needs **AWS CLI v2.9 or later** — the `[sso-session]` block below is not
understood by v1 or by early v2 builds, and the failure looks like a confusing
profile error rather than a version complaint.

```bash
aws --version        # expect aws-cli/2.x, 2.9 or newer
```

Append to `~/.aws/config`:

```ini
[sso-session non-prod-sso]
sso_start_url = https://identitycenter.amazonaws.com/ssoins-668463dde3ff5e4e
sso_region = us-east-2
sso_registration_scopes = sso:account:access

[profile non-prod-sso]
sso_session = non-prod-sso
sso_account_id = 147723036280
sso_role_name = NonProdDeveloper
region = us-east-2
output = json
```

`https://d-9a67580b1d.awsapps.com/start` is an alias for the same Identity Center
instance and works identically as `sso_start_url`.

```bash
aws sso login --profile non-prod-sso
export AWS_PROFILE=non-prod-sso
aws sts get-caller-identity          # expect 147723036280
```

A browser opens for Google sign-in. `AWS_PROFILE` lasts for that shell only —
put it in your shell profile, or pass `--profile non-prod-sso` on every command.

Sessions last 8 hours. `deploy.sh` and `test_feed.py` both check the account
before doing anything, so a stale or wrong-account session fails loudly rather
than deploying somewhere unexpected.

> **One IAM caveat.** The stack creates its own execution role. `NonProdDeveloper`
> is described as full create/write/delete, but IAM changes are listed against
> `NonProdAdmin` — so `iam:CreateRole` may be refused under the developer role. If
> the deploy fails on the role, re-run it with `sso_role_name = NonProdAdmin`
> (1-hour session). This is the one step that may need the elevated set.

### Put the #slack-test webhook in SSM

Add an incoming webhook for **#slack-test** to the existing "S3 Feed Monitor"
Slack app, then store it in this account:

```bash
aws ssm put-parameter \
  --name /platform-monitors/s3-feed-freshness/slack-test-webhook \
  --value 'https://hooks.slack.com/services/...' \
  --type SecureString --region us-east-2
```

The path is `SLACK_WEBHOOK_PARAM` in `env/nonprod.env`. Nothing else reads it,
and the value never enters the template or the repo.

### The run

```bash
cd cfn

python3 test_feed.py create      # scratch bucket, public access blocked, tagged
./deploy.sh nonprod              # dry-run: logs the payload, posts nothing

aws lambda invoke --function-name s3-feed-freshness-nonprod \
  --region us-east-2 /dev/stdout | jq '.webhook_check, .feeds_stale'
```

`webhook_check.ok` must read `true` before going further — that is the whole
point of the dry run. Then:

```bash
./deploy.sh nonprod false        # live
```

`env/nonprod.env` sets `rate(30 minutes)`, so you never wait more than half an
hour for a result. Drive the two transitions by hand:

```bash
python3 test_feed.py clear       # empty prefix  -> next run posts a STALE alert
python3 test_feed.py drop        # one file      -> next run posts the recovery
python3 test_feed.py status      # what the monitor will see on its next run
```

**Why an empty prefix rather than old files.** S3 will not let you backdate
`LastModified`, so ageing a file into staleness is not possible. An empty prefix
is: the monitor reports it as "no files ever", which is past any threshold and so
stale by definition. Dropping one file puts the feed at 0 days idle and clears
it. Both transitions, no waiting — and because neither depends on the threshold,
non-prod runs prod's real `StaleDays=3` rather than a lowered one.

`RENOTIFY_HOURS=0` in non-prod means one alert per outage rather than a repeat
every thirty minutes, so #slack-test stays readable.

### Clean up when you are done

Non-prod is not free, though this stack is close to it — a Lambda on a 30-minute
schedule and a nearly-empty bucket. Still:

```bash
aws cloudformation delete-stack --stack-name s3-feed-freshness-nonprod --region us-east-2
aws s3 rb s3://s3-feed-freshness-testfeed-147723036280 --force --region us-east-2
```

Or leave the stack and disable the EventBridge rule if you want to come back to it.


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
policy. 15 statements, every one scoped to `s3-feed-freshness*` in us-east-2,
`PassRole` conditioned to `lambda.amazonaws.com`. Administrator access is not
required.

Two of those statements are not about this stack's resources, and are the two
easiest to leave out by accident:

- `TemplateInspectionIsNotResourceScopable` — `ValidateTemplate` and
  `GetTemplateSummary` take no resource ARN, so they can only be granted on `*`.
- `AllowTheServerlessTransform` — the template's `Transform:
  AWS::Serverless-2016-10-31` is a macro CloudFormation hosts, and expanding it
  is authorised as `cloudformation:CreateChangeSet` against
  `arn:aws:cloudformation:us-east-2:aws:transform/Serverless-2016-10-31`, not
  against the stack. Without it the deploy fails during expansion with an
  AccessDenied naming the transform rather than anything in this repo.

`deploy.sh` passes `CAPABILITY_IAM CAPABILITY_AUTO_EXPAND`. The IAM capability
is for the execution role; the auto-expand one is for that same SAM transform.
Change-set deploys do not always demand it and stack-level ones do, and it is
inert when unnecessary — so it is always passed rather than diagnosed.

The policy provisions its own artifact bucket,
`s3-feed-freshness-artifacts-057311931122`, so there is no dependency on
discovering an existing one. Create it once after the policy is attached:

```bash
aws s3 mb s3://s3-feed-freshness-artifacts-057311931122 --region us-east-2
```

If your team already has a shared CloudFormation artifact bucket, swap both
ARNs in the `CreateAndUseTheArtifactBucket` statement for that bucket instead
and drop `s3:CreateBucket`.

```bash
aws iam create-policy --policy-name s3-feed-freshness-deploy \
  --policy-document file://cfn/deploy-policy.json
aws iam attach-user-policy --user-name eniyavant \
  --policy-arn arn:aws:iam::057311931122:policy/s3-feed-freshness-deploy
```

Confirmed as of 09/03/26: user `eniyavant` has no CloudFormation access at all —
even `cloudformation:ValidateTemplate` is denied — so one of these two routes is
mandatory before any deploy step will work.

---

## Open questions

Three things this repo assumes that nothing has confirmed. Each is a parameter,
so none of them blocks writing the code — but the first two decide whether the
first live run reaches anybody.

**1. The platform webhook's real SSM path.** `/platform/slack/webhook-dam-alerts`
is a placeholder chosen to look like the thing it stands for. No parameter under
`/platform/slack/` has been confirmed to exist. Step 1 above finds the real one;
step 3 proves the monitor can read it.

**2. The channel.** The existing platform monitors — RDS zero-connections, the
staging-env reaper, the access-key rotation enforcer — all post to
**#platform-team**, where the platform engineers read them and reply in thread.
**#dam-alerts** is where the DAM application's own job log goes: import started,
import completed, brand sync finished, dozens of machine messages a day and no
human replies. A feed-staleness alert posted there is unlikely to be read. Worth
settling before go-live; it is one parameter either way.

**3. Whether `aws-infra` exists.** The preferred route hands this folder to that
repo's pipeline, which needs no IAM grant to anyone. The repo name appears
exactly once anywhere searchable — in the message proposing this design —
so its existence, its pipeline, and that pipeline's permissions are all
unconfirmed. If it does not exist, the hand-deploy route and its policy are the
only path.
