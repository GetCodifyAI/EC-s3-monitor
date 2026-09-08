# Request — a scoped deploy policy for the S3 staleness monitor

**Requester** Eniyavan · **Account** 147723036280 (Cut+Dry Eng, non-prod) ·
**Region** us-east-2 · **Repo** GetCodifyAI/EC-s3-monitor · **Updated** 09/08/26

## Status: deployed and live. This asks for something narrower than it used to.

The monitor is running. A daily Lambda alerts #slack-test when
`s3://cut-and-dry-test/test-dev/` receives no new file for three consecutive
days. First live alert posted 09/08/26 08:34 and the whole path is proven:
schedule, IAM role, S3 listing, SSM secret, webhook, Slack.

The original blocker - `NonProdDeveloper` has no `iam:CreateRole` - was cleared
by granting me `NonProdAdmin`, and the stack was deployed under that role.

**That is the thing this document now asks to fix.** Every deploy of this stack
currently needs break-glass admin. That is a poor steady state for a routine
platform monitor: a 1-hour session, a role scoped far wider than the task, and
an ordinary config change - adding a prefix, moving the channel, adjusting the
threshold - that cannot be made without it.

## What I'm asking for

Create the customer-managed policy in account 147723036280:

```bash
aws iam create-policy --policy-name s3-staleness-monitor-deploy \
  --policy-document file://deploy-policy.json
```

Then in IAM Identity Center: **Permission sets → NonProdDeveloper → Customer
managed policies → Attach → `s3-staleness-monitor-deploy`**, then **Provision**
to 147723036280.

Identity Center references a customer managed policy *by name*, so it must
exist in every account the permission set is provisioned to.

**The tradeoff to weigh:** attaching to `NonProdDeveloper` gives every developer
with that permission set these rights over this one stack. If that is more than
you want, an alternative is a dedicated permission set for platform-monitor
deploys, or leaving it as-is and accepting the break-glass step. I have no
strong preference beyond not wanting admin to be the routine path.

## Why the policy is safe to approve

`deploy-policy.json` in this repo - 13 statements, 3,980 characters minified,
against IAM's 6,144-character limit.

- **Name-scoped.** Every statement targets `s3-staleness-monitor*` resources in
  `us-east-2` in this account. It cannot touch another stack, function, role,
  rule or alarm.
- **Not a privilege-escalation path.** It grants `iam:PutRolePolicy` but
  deliberately *not* `iam:AttachRolePolicy`, so it cannot attach
  `AdministratorAccess` to anything. It cannot create users, and cannot create a
  role for a person - `iam:PassRole` is conditioned on
  `iam:PassedToService = lambda.amazonaws.com`.
- **No data access.** The only S3 write access is to a dedicated artifact bucket
  the policy itself provisions. Against `cut-and-dry-test` it grants
  `ListBucket` and `GetBucketLocation` - never `GetObject`.
- **No secret access.** Against SSM it grants `DescribeParameters` only, which
  returns names and types and never values. The deployed Lambda reads the
  webhook at runtime; the person deploying it cannot.
- **It is strictly narrower than what deployed the stack.** `NonProdAdmin` did
  this on 09/08/26. This policy is a subset of that, scoped to one stack.
- **The deployed monitor is narrower still.** Its runtime role has four
  statements: list one bucket, read one SSM parameter, decrypt via SSM, write
  its own logs. No `GetObject`, no `PutObject` - it cannot read a row of
  whatever lands in the prefix, and cannot write into the prefix it watches.
- **The monitored bucket is untouched.** No notification config is added or
  changed - `trigger_sync` and the existing EventBridge config are not
  disturbed. That is why this polls rather than subscribing.

Verified against the real deploy: the policy was written before the stack
existed, so if anything in it is wrong it is likely to be a missing action
rather than an excess one. A denial names the action and it can be added.

## Cost

Effectively zero. Roughly 30 Lambda invocations a month at 256 MB sits inside
the free tier; the log group at 30-day retention is the only standing cost.

## Two things still open, both more important than the above

1. **No SNS topic for the monitor's own health alarm.** `AlarmTopicArn` is
   empty, so nothing watches the monitor. Its healthy state is silence, which
   means a crashed Lambda and a working feed look identical from the channel.
   This is the real gap. One topic ARN closes it.
2. **#slack-test is the wrong permanent home.** It also carries the DAM export
   log and the daily Scraper Health Report - twenty-plus machine messages a day.
   An alert designed to be silent-unless-broken earns very little there. It was
   the right place to prove the plumbing. Where should it live?

## Detail

Design, deployment and triage: `RUNBOOK.md`.
