# Access request — S3 staleness monitor

**Requester** Eniyavan · **Account** 147723036280 (Cut+Dry Eng, non-prod) ·
**Region** us-east-2 · **Repo** GetCodifyAI/EC-s3-monitor · **Date** 09/04/26

## What this is

A daily Lambda that alerts #slack-test when `s3://cut-and-dry-test/test-dev/`
receives no new file for three consecutive days.

Everything is built and verified except the deploy itself:

- bucket confirmed readable from this account
- Slack webhook created and proven end to end — a test message posted to
  #slack-test successfully
- webhook stored as an SSM SecureString
- 30 unit tests and a 6-scenario offline harness, all passing

The deploy fails at one step. `NonProdDeveloper` has no `iam:CreateRole`:

```
User: arn:aws:sts::147723036280:assumed-role/AWSReservedSSO_NonProdDeveloper_.../eniyavant@cutanddry.com
is not authorized to perform: iam:CreateRole on resource:
arn:aws:iam::147723036280:role/s3-staleness-monitor-MonitorRole-*
because no identity-based policy allows the iam:CreateRole action
```

CloudFormation reports this as `UnauthorizedTaggingOperation`, which is
misleading — the real reason is nested inside it.

`NonProdAdmin` is not assigned to me, so break-glass is not an option either:
`GetRoleCredentials ... No access`.

## Option A — an admin runs the deploy once (fastest)

```bash
aws sso login --profile non-prod-admin
cd EC-s3-monitor
PROFILE=non-prod-admin ./deploy.sh nonprod
```

`DryRun` defaults to `true`, so this deploys inert — the Lambda logs the message
it would post and sends nothing to Slack. Takes about two minutes. Once the IAM
role exists, I can run every subsequent deploy under `NonProdDeveloper` myself.

The catch: any future change to the role's permissions needs an admin again.

## Option B — attach a scoped deploy policy (preferred)

One customer-managed policy, no admin rights. In account 147723036280:

```bash
aws iam create-policy --policy-name s3-staleness-monitor-deploy \
  --policy-document file://deploy-policy.json
```

Then in IAM Identity Center: **Permission sets → NonProdDeveloper → Customer
managed policies → Attach → `s3-staleness-monitor-deploy`**, then **Provision**
to account 147723036280.

Identity Center references a customer managed policy *by name*, so the policy
must exist in each account the permission set is provisioned to. Attaching it
account-wide to `NonProdDeveloper` would give every developer these rights over
this one stack, which may be more than you want — if so, Option A is cleaner and
I will come back for changes.

## Why the policy is safe to approve

The document is in this repo at `deploy-policy.json` — 13 statements, 3,980
characters minified, against IAM's 6,144-character limit.

- **Name-scoped.** Every statement targets `s3-staleness-monitor*` resources in
  `us-east-2` in this account. It cannot touch another stack, function, role,
  rule or alarm.
- **Not a privilege-escalation path.** It grants `iam:PutRolePolicy` but
  deliberately *not* `iam:AttachRolePolicy`, so it cannot attach
  `AdministratorAccess` to anything. It cannot create users, and cannot create a
  role for a person — `iam:PassRole` is conditioned on
  `iam:PassedToService = lambda.amazonaws.com`.
- **No data access.** The only S3 write access is to a dedicated artifact bucket
  the policy itself provisions. Against `cut-and-dry-test` it grants
  `ListBucket` and `GetBucketLocation` — never `GetObject`.
- **No secret access.** Against SSM it grants `DescribeParameters` only, which
  returns names and types and never values. The deployed Lambda reads the
  webhook at runtime; the person deploying it cannot.
- **The deployed monitor is narrower still.** Its runtime role has four
  statements: list one bucket, read one SSM parameter, decrypt via SSM, write
  its own logs. No `GetObject`, no `PutObject` — it cannot read a row of
  whatever lands in the prefix, and cannot write into the prefix it watches.
- **Deploys inert.** `DryRun` defaults to `true` and has to be flipped
  explicitly.
- **The monitored bucket is untouched.** No notification config is added or
  changed — `trigger_sync` and the existing EventBridge config are not
  disturbed. That is why this polls rather than subscribing.

If a permissions boundary on created roles is required, say so and I will add a
`PermissionsBoundary` property to the template — it is a two-line change.

## Cost

Effectively zero. 30 Lambda invocations a month at 128 MB sits inside the free
tier; the log group is the only standing cost and it is capped at 30 days
retention. No alarm is created unless an SNS topic is supplied.

## What I still need regardless of which option

1. **An SNS topic for the monitor's own health alarm.** Right now
   `AlarmTopicArn` is empty, which means nothing watches the monitor. A silent
   channel currently cannot be distinguished from a crashed Lambda — that is the
   real gap, more than the deploy permission.
2. **A view on the channel.** #slack-test also carries the DAM bot's export log,
   dozens of messages a day, enough that Slack rate-limits it. An alert designed
   to be silent-unless-broken loses most of its value there. Is there a better
   home for it?

## Detail

Design, deployment and triage: `RUNBOOK.md` in this repo.
