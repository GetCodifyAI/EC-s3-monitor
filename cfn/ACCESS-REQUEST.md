# Access request — S3 feed freshness monitor

**Requester** Eniyavan · **Account** 057311931122 · **Region** us-east-2
**Repo** GetCodifyAI/EC-s3-monitor · **Date** 09/03/26

## What this is

A daily Lambda that alerts #dam-alerts when a vendor feed prefix in
`s3://cut-dry-vendor-integration/` stops receiving files. The PO feed has had no
drop for roughly 13 business days and nothing detected it — that gap is what
this closes.

The code is written, reviewed and merged. It cannot be deployed because my IAM
user has no CloudFormation access.

> **If an admin is deploying this directly**, none of the IAM grant below is
> needed — they already hold the permissions. Skip to
> [What I still need regardless](#what-i-still-need-regardless), which is the
> only part that still applies, and follow *First deployment* in `RUNBOOK.md`.
> The policy is documented here for the case where the deploy is delegated back.

## What I'm asking for

Attach one customer-managed policy to IAM user `eniyavant`:

```bash
aws iam create-policy --policy-name s3-feed-freshness-deploy \
  --policy-document file://cfn/deploy-policy.json

aws iam attach-user-policy --user-name eniyavant \
  --policy-arn arn:aws:iam::057311931122:policy/s3-feed-freshness-deploy
```

**Administrator access is not required.** The policy is in this repo at
`cfn/deploy-policy.json` — 15 statements, 4,316 characters minified,
against IAM's 6,144-character managed-policy limit.

## Why it is safe to approve

- **Name-scoped.** Every statement targets `s3-feed-freshness*` resources in
  `us-east-2` only. It cannot touch any other stack, function, role or alarm.
- **`PassRole` is conditioned** to `iam:PassedToService = lambda.amazonaws.com`.
  It cannot create a role for a person, and cannot attach managed policies to
  anything — so it is not a privilege-escalation path.
- **No data access.** The only S3 write access is to a dedicated artifact bucket
  the policy itself provisions. Against the vendor bucket it grants
  `ListBucket` and nothing else — no `GetObject`, ever.
- **The deployed monitor is narrower still.** Its runtime role has six
  statements: list one prefix, read/write its own SSM watermark, read the Slack
  webhook parameter, decrypt via SSM, and write its own logs. It cannot read a
  single row of vendor data.
- **No secret access.** Against the Slack webhook parameter the deploy policy
  grants `DescribeParameters` — names and types, not values. The deployed
  Lambda reads the webhook; the person deploying it never does.
- **Deploys inert.** `DryRun` defaults to `true`; the Lambda logs the message it
  would post and sends nothing until that is flipped.

## Cost

Under $0.30/month — effectively just the two CloudWatch alarms at $0.10 each.
30 Lambda invocations a month sits inside the free tier.

## The alternative, if standing deploy rights are unwanted

This was built to match the RDS zero-connections and EC2 over-provisioned
platform monitors, so it can be ported into `aws-infra` and deployed by that
pipeline's role instead. That needs no grant to me at all — I just need to know
the repo exists and where it lives. **This is my preferred option.**

## What I still need regardless

1. The SSM parameter name holding the platform Slack incoming webhook, and
   confirmation it posts to #dam-alerts.
2. Confirmation of whether `aws-infra` exists and has a deploy pipeline.
3. An existing SNS topic for the monitor-health alarms, if there is one.

## Detail

Full design, triage steps and operations are in `cfn/RUNBOOK.md`.
