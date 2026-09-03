#!/usr/bin/env bash
# Package and deploy the S3 feed freshness monitor.
#
#   ./deploy.sh <artifact-bucket> [stack-name] [dry-run]
#
#   ./deploy.sh my-cfn-artifacts                      # dry-run, default stack
#   ./deploy.sh my-cfn-artifacts s3-feed-freshness false   # go live
#
set -euo pipefail

ARTIFACT_BUCKET="${1:-s3-feed-freshness-artifacts-057311931122}"
STACK="${2:-s3-feed-freshness}"
DRY_RUN="${3:-true}"
REGION="${AWS_REGION:-us-east-2}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

echo "==> packaging"
aws cloudformation package \
  --template-file template.yaml \
  --s3-bucket "$ARTIFACT_BUCKET" \
  --s3-prefix "$STACK" \
  --output-template-file .packaged.yaml \
  --region "$REGION"

echo "==> deploying $STACK (DryRun=$DRY_RUN)"
aws cloudformation deploy \
  --template-file .packaged.yaml \
  --stack-name "$STACK" \
  --capabilities CAPABILITY_IAM \
  --region "$REGION" \
  --parameter-overrides \
      DryRun="$DRY_RUN" \
      ${ALARM_TOPIC_ARN:+AlarmTopicArn="$ALARM_TOPIC_ARN"} \
      ${FEEDS_JSON:+FeedsJson="$FEEDS_JSON"} \
      ${SLACK_WEBHOOK_PARAM:+SlackWebhookParam="$SLACK_WEBHOOK_PARAM"} \
  --tags Project=platform-monitors Monitor=s3-feed-freshness Owner=data-eng

echo "==> outputs"
aws cloudformation describe-stacks --stack-name "$STACK" --region "$REGION" \
  --query 'Stacks[0].Outputs[].[OutputKey,OutputValue]' --output table

if [ "$DRY_RUN" = "true" ]; then
  cat <<'MSG'

Deployed in DRY-RUN. Nothing will reach Slack yet.
Invoke it and read the payload it would have posted:

  aws lambda invoke --function-name <FunctionName> --region us-east-2 /dev/stdout | jq
  aws logs tail /aws/lambda/<FunctionName> --since 10m --region us-east-2 | grep -A40 DRY_RUN

When the output looks right, re-run this script with dry-run = false.
MSG
fi
