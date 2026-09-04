#!/usr/bin/env bash
#
# Package and deploy the S3 staleness monitor.
#
#   ./deploy.sh nonprod          # dry run - nothing reaches Slack
#   ./deploy.sh nonprod false    # live
#
# Dry run is the default on purpose: the harmless mode should be the one you get
# by forgetting an argument.
set -euo pipefail

ENV_NAME="${1:-}"
DRY_RUN="${2:-true}"

if [[ -z "$ENV_NAME" ]]; then
  echo "usage: $0 <env> [dry_run]     e.g. $0 nonprod false" >&2
  echo "environments: $(cd "$(dirname "$0")/env" && ls *.env | sed 's/\.env//' | tr '\n' ' ')" >&2
  exit 2
fi

if [[ "$DRY_RUN" != "true" && "$DRY_RUN" != "false" ]]; then
  echo "error: dry_run must be 'true' or 'false', got '$DRY_RUN'" >&2
  exit 2
fi

cd "$(dirname "$0")"
ENV_FILE="env/${ENV_NAME}.env"
[[ -f "$ENV_FILE" ]] || { echo "error: no such environment file: $ENV_FILE" >&2; exit 2; }

# An explicit PROFILE in the environment wins over the one in the env file, so
# the first deploy can be run under a break-glass role without editing config
# that is committed:  PROFILE=non-prod-admin ./deploy.sh nonprod
PROFILE_OVERRIDE="${PROFILE:-}"

# shellcheck disable=SC1090
set -a; source "$ENV_FILE"; set +a

if [[ -n "$PROFILE_OVERRIDE" && "$PROFILE_OVERRIDE" != "${PROFILE:-}" ]]; then
  echo "==> Profile overridden from the environment: $PROFILE_OVERRIDE (env file says ${PROFILE:-none})"
  PROFILE="$PROFILE_OVERRIDE"
fi

AWS=(aws --region "$REGION")
[[ -n "${PROFILE:-}" ]] && AWS+=(--profile "$PROFILE")

# ---------------------------------------------------------------------------
# Account guard.
#
# The whole point: non-prod parameters must never be able to land on a stack in
# another account. Checked before anything is created, so a wrong profile costs
# you an error message rather than a deployment.
# ---------------------------------------------------------------------------
echo "==> Checking credentials"
CALLER_JSON=$("${AWS[@]}" sts get-caller-identity --output json 2>/dev/null) || {
  echo "error: no valid AWS credentials." >&2
  echo "       run: aws sso login --profile ${PROFILE:-<your-profile>}" >&2
  exit 1
}
CALLER_ACCOUNT=$(echo "$CALLER_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["Account"])')
CALLER_ARN=$(echo "$CALLER_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["Arn"])')

if [[ "$CALLER_ACCOUNT" != "$ACCOUNT" ]]; then
  echo "error: '$ENV_NAME' targets account $ACCOUNT but your credentials are for $CALLER_ACCOUNT." >&2
  echo "       $CALLER_ARN" >&2
  exit 1
fi
echo "    account $CALLER_ACCOUNT, region $REGION"
echo "    $CALLER_ARN"

# ---------------------------------------------------------------------------
# Preflight: the webhook parameter must exist before the stack references it.
#
# describe-parameters returns names and types, never values, so this confirms
# the parameter is there without anyone having to read the secret. Whether the
# value is well-formed is the dry run's job.
# ---------------------------------------------------------------------------
echo "==> Checking the Slack webhook parameter"
FOUND=$("${AWS[@]}" ssm describe-parameters \
  --parameter-filters "Key=Name,Values=${SLACK_WEBHOOK_PARAM}" \
  --query 'Parameters[0].Type' --output text 2>/dev/null || echo "None")
if [[ "$FOUND" == "None" || -z "$FOUND" ]]; then
  echo "error: no SSM parameter at ${SLACK_WEBHOOK_PARAM}" >&2
  echo "       create it first - see RUNBOOK.md step 2." >&2
  exit 1
fi
if [[ "$FOUND" != "SecureString" ]]; then
  echo "error: ${SLACK_WEBHOOK_PARAM} is type ${FOUND}, expected SecureString." >&2
  echo "       a webhook URL is a bearer token; store it encrypted." >&2
  exit 1
fi
echo "    ${SLACK_WEBHOOK_PARAM} exists, SecureString"

echo "==> Ensuring artifact bucket"
if ! "${AWS[@]}" s3api head-bucket --bucket "$ARTIFACT_BUCKET" >/dev/null 2>&1; then
  echo "    creating s3://${ARTIFACT_BUCKET}"
  "${AWS[@]}" s3api create-bucket --bucket "$ARTIFACT_BUCKET" \
    --create-bucket-configuration "LocationConstraint=${REGION}" >/dev/null
  "${AWS[@]}" s3api put-public-access-block --bucket "$ARTIFACT_BUCKET" \
    --public-access-block-configuration \
    "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true" >/dev/null
  "${AWS[@]}" s3api put-bucket-encryption --bucket "$ARTIFACT_BUCKET" \
    --server-side-encryption-configuration \
    '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}' >/dev/null
else
  echo "    s3://${ARTIFACT_BUCKET} exists"
fi

echo "==> Packaging"
# Keep compiled bytecode out of the deployment zip. Running the tests locally
# leaves a src/__pycache__ behind, and cloudformation package would ship it.
rm -rf src/__pycache__
PACKAGED=$(mktemp -t packaged.XXXXXX.yaml)
trap 'rm -f "$PACKAGED"' EXIT
"${AWS[@]}" cloudformation package \
  --template-file template.yaml \
  --s3-bucket "$ARTIFACT_BUCKET" \
  --s3-prefix "$STACK" \
  --output-template-file "$PACKAGED" >/dev/null
echo "    packaged"

echo "==> Deploying stack '$STACK'  (DryRun=${DRY_RUN})"
"${AWS[@]}" cloudformation deploy \
  --template-file "$PACKAGED" \
  --stack-name "$STACK" \
  --capabilities CAPABILITY_IAM \
  --no-fail-on-empty-changeset \
  --tags "Owner=eniyavan" "Project=s3-staleness-monitor" "Environment=${ENV_NAME}" \
  --parameter-overrides \
    "MonitoredBucket=${MONITORED_BUCKET}" \
    "FeedsJson=${FEEDS_JSON}" \
    "StaleDays=${STALE_DAYS}" \
    "BusinessDaysOnly=${BUSINESS_DAYS_ONLY}" \
    "ObjectSuffix=${OBJECT_SUFFIX}" \
    "SlackWebhookParam=${SLACK_WEBHOOK_PARAM}" \
    "DryRun=${DRY_RUN}" \
    "ScheduleExpression=${SCHEDULE_EXPRESSION}" \
    "DisplayTimeZone=${DISPLAY_TZ}" \
    "LogRetentionDays=${LOG_RETENTION_DAYS}" \
    "AlarmTopicArn=${ALARM_TOPIC_ARN}"

echo
"${AWS[@]}" cloudformation describe-stacks --stack-name "$STACK" \
  --query 'Stacks[0].Outputs' --output table

if [[ "$DRY_RUN" == "true" ]]; then
  cat <<EOF

Deployed in DRY RUN. Nothing will reach Slack.
Run it now and read what it would have posted:

  aws lambda invoke --function-name $STACK --region $REGION \\
    ${PROFILE:+--profile $PROFILE }/dev/stdout | python3 -m json.tool

  aws logs tail /aws/lambda/$STACK --since 10m --region $REGION ${PROFILE:+--profile $PROFILE}

When the output looks right:  ./deploy.sh $ENV_NAME false
EOF
else
  cat <<EOF

LIVE. The next stale run posts to #slack-test.
Back to dry run at any time:  ./deploy.sh $ENV_NAME
EOF
fi
