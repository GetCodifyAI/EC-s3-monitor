#!/usr/bin/env bash
# Package and deploy the S3 feed freshness monitor into one environment.
#
#   ./deploy.sh <env> [dry-run]
#
#   ./deploy.sh nonprod           # dry-run into 147723036280, posts nothing
#   ./deploy.sh nonprod false     # live in non-prod, posts to #slack-test
#   ./deploy.sh prod              # dry-run into 057311931122
#   ./deploy.sh prod false        # live in prod
#
# Environments are files in env/. Each one pins the account it belongs to, and
# this script refuses to run when the credentials in your shell point somewhere
# else - so non-prod parameters can never land on the prod stack, or the reverse.
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

ENV_NAME="${1:-}"
DRY_RUN="${2:-true}"

if [ -z "$ENV_NAME" ]; then
  echo "usage: ./deploy.sh <env> [dry-run]" >&2
  echo "environments:" >&2
  for f in env/*.env; do echo "  $(basename "$f" .env)" >&2; done
  exit 2
fi

ENV_FILE="env/${ENV_NAME}.env"
if [ ! -f "$ENV_FILE" ]; then
  echo "no such environment: $ENV_NAME (looked for $ENV_FILE)" >&2
  exit 2
fi

case "$DRY_RUN" in
  true|false) ;;
  *) echo "dry-run must be 'true' or 'false', got '$DRY_RUN'" >&2; exit 2 ;;
esac

# shellcheck disable=SC1090
set -a; . "./$ENV_FILE"; set +a

for required in ACCOUNT REGION STACK ARTIFACT_BUCKET MONITORED_BUCKET FEEDS_JSON SLACK_WEBHOOK_PARAM; do
  if [ -z "${!required:-}" ]; then
    echo "$ENV_FILE is missing $required" >&2
    exit 2
  fi
done

# ------------------------------------------------------------- account guard
echo "==> checking who you are"
CALLER_JSON="$(aws sts get-caller-identity --region "$REGION" --output json)"
CALLER_ACCOUNT="$(printf '%s' "$CALLER_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["Account"])')"
CALLER_ARN="$(printf '%s' "$CALLER_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["Arn"])')"

if [ "$CALLER_ACCOUNT" != "$ACCOUNT" ]; then
  cat >&2 <<MISMATCH

REFUSING TO DEPLOY.

  environment   $ENV_NAME
  targets       $ACCOUNT
  your creds    $CALLER_ACCOUNT
  as            $CALLER_ARN

Either you picked the wrong environment or you are signed in to the wrong
account. For non-prod:  aws sso login --profile non-prod-sso

MISMATCH
  exit 1
fi

echo "    $CALLER_ARN"
echo "    account $CALLER_ACCOUNT matches env/$ENV_NAME.env"

# ------------------------------------------------------- artifact bucket
# cloudformation package needs somewhere to put the code zip. Name-scoped, and
# the deploy policy grants s3:CreateBucket for exactly this bucket.
if ! aws s3api head-bucket --bucket "$ARTIFACT_BUCKET" --region "$REGION" 2>/dev/null; then
  echo "==> creating artifact bucket $ARTIFACT_BUCKET"
  aws s3api create-bucket \
    --bucket "$ARTIFACT_BUCKET" \
    --region "$REGION" \
    --create-bucket-configuration LocationConstraint="$REGION" >/dev/null
fi

echo "==> packaging $STACK"
aws cloudformation package \
  --template-file template.yaml \
  --s3-bucket "$ARTIFACT_BUCKET" \
  --s3-prefix "$STACK" \
  --output-template-file ".packaged.${ENV_NAME}.yaml" \
  --region "$REGION"

echo "==> deploying $STACK to $ACCOUNT (DryRun=$DRY_RUN)"
aws cloudformation deploy \
  --template-file ".packaged.${ENV_NAME}.yaml" \
  --stack-name "$STACK" \
  --capabilities CAPABILITY_IAM CAPABILITY_AUTO_EXPAND \
  --region "$REGION" \
  --parameter-overrides \
      DryRun="$DRY_RUN" \
      MonitoredBucket="$MONITORED_BUCKET" \
      FeedsJson="$FEEDS_JSON" \
      SlackWebhookParam="$SLACK_WEBHOOK_PARAM" \
      StaleDays="${STALE_DAYS:-3}" \
      BusinessDaysOnly="${BUSINESS_DAYS_ONLY:-true}" \
      RenotifyHours="${RENOTIFY_HOURS:-24}" \
      ScheduleExpression="${SCHEDULE_EXPRESSION:-cron(0 15 * * ? *)}" \
      ${ALARM_TOPIC_ARN:+AlarmTopicArn="$ALARM_TOPIC_ARN"} \
  --tags Project=platform-monitors Monitor=s3-feed-freshness Owner=data-eng Environment="$ENV_NAME"

echo "==> outputs"
aws cloudformation describe-stacks --stack-name "$STACK" --region "$REGION" \
  --query 'Stacks[0].Outputs[].[OutputKey,OutputValue]' --output table

if [ "$DRY_RUN" = "true" ]; then
  cat <<MSG

Deployed in DRY-RUN. Nothing will reach Slack yet.
Invoke it and read the payload it would have posted, plus webhook_check:

  aws lambda invoke --function-name $STACK --region $REGION /dev/stdout | jq
  aws logs tail /aws/lambda/$STACK --since 10m --region $REGION | grep -A40 DRY_RUN

When webhook_check reads ok:true and the message looks right:

  ./deploy.sh $ENV_NAME false
MSG
fi
