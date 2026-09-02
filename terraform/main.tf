###############################################################################
# S3 feed staleness monitor -> Slack
# Alerts when no new PO file lands in the Enterprise Cafe incoming prefix
# for N consecutive days.
###############################################################################

terraform {
  required_version = ">= 1.5"
  required_providers {
    aws     = { source = "hashicorp/aws", version = "~> 5.0" }
    archive = { source = "hashicorp/archive", version = "~> 2.4" }
  }
}

provider "aws" {
  region = var.region
}

# ------------------------------------------------------------------- variables
variable "region" {
  type    = string
  default = "us-east-2"
}

variable "monitor_id" {
  type    = string
  default = "enterprise-cafe-po-prod"
}

variable "bucket" {
  type    = string
  default = "cut-dry-vendor-integration"
}

variable "prefix" {
  type    = string
  default = "enterprise-cafe/prod/incoming/purchase-orders/"
}

variable "suffix" {
  description = "Only objects with this suffix count as a delivery. Empty string = all objects."
  type        = string
  default     = ".csv"
}

variable "stale_days" {
  type    = number
  default = 3
}

variable "business_days_only" {
  description = "Count Mon-Fri only, so a Friday drop does not alert on Monday."
  type        = bool
  default     = false
}

variable "renotify_hours" {
  description = "Re-ping Slack this often while stale. 0 = one alert per outage."
  type        = number
  default     = 24
}

variable "schedule_expression" {
  description = "When to run the check. 15:00 UTC = 08:00 PT."
  type        = string
  default     = "cron(0 15 * * ? *)"
}

variable "webhook_source" {
  description = "Where the Lambda reads the Slack webhook from: secretsmanager | ssm | env"
  type        = string
  default     = "ssm"

  validation {
    condition     = contains(["secretsmanager", "ssm", "env"], var.webhook_source)
    error_message = "webhook_source must be secretsmanager, ssm, or env."
  }
}

variable "webhook_url" {
  description = "Only used when webhook_source = env. Pass via TF_VAR_webhook_url, never a committed tfvars."
  type        = string
  default     = ""
  sensitive   = true
}

variable "display_tz" {
  type    = string
  default = "America/Los_Angeles"
}

variable "tags" {
  type    = map(string)
  default = {}
}

data "aws_caller_identity" "current" {}

locals {
  name = "s3-feed-monitor-${var.monitor_id}"

  webhook_env = (
    var.webhook_source == "secretsmanager"
    ? { SLACK_SECRET_ARN = aws_secretsmanager_secret.slack[0].arn }
    : var.webhook_source == "ssm"
    ? { SLACK_SSM_PARAM = aws_ssm_parameter.slack[0].name }
    : { SLACK_WEBHOOK_URL = var.webhook_url }
  )
}

# ---------------------------------------------------------------------- secret
# Created empty on purpose: put the webhook in with the CLI so it never
# lands in Terraform state or a .tfvars file.
resource "aws_secretsmanager_secret" "slack" {
  count       = var.webhook_source == "secretsmanager" ? 1 : 0
  name        = "${local.name}/slack-webhook"
  description = "Slack incoming webhook URL for ${var.monitor_id} feed alerts"
  tags        = var.tags
}

# SSM SecureString alternative. Value is set out of band with the CLI, so
# lifecycle ignores it and it never enters Terraform state.
resource "aws_ssm_parameter" "slack" {
  count  = var.webhook_source == "ssm" ? 1 : 0
  name   = "/${local.name}/slack-webhook"
  type   = "SecureString"
  value  = "PLACEHOLDER_SET_VIA_CLI"
  tags   = var.tags

  lifecycle {
    ignore_changes = [value]
  }
}

# ------------------------------------------------------------------ state table
resource "aws_dynamodb_table" "state" {
  name         = "${local.name}-state"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "monitor_id"

  attribute {
    name = "monitor_id"
    type = "S"
  }

  point_in_time_recovery { enabled = false }
  tags                   = var.tags
}

# -------------------------------------------------------------------------- iam
data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda" {
  name               = "${local.name}-role"
  assume_role_policy = data.aws_iam_policy_document.assume.json
  tags               = var.tags
}

data "aws_iam_policy_document" "lambda" {
  statement {
    sid       = "ListMonitoredPrefix"
    actions   = ["s3:ListBucket"]
    resources = ["arn:aws:s3:::${var.bucket}"]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["${var.prefix}*"]
    }
  }

  statement {
    sid       = "MonitorState"
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem"]
    resources = [aws_dynamodb_table.state.arn]
  }

  statement {
    sid       = "PublishMetrics"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = ["CutAndDry/FeedFreshness"]
    }
  }

  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.lambda.arn}:*"]
  }
}

resource "aws_iam_role_policy" "lambda" {
  name   = "${local.name}-policy"
  role   = aws_iam_role.lambda.id
  policy = data.aws_iam_policy_document.lambda.json
}

# Webhook-read permission, only for the source actually in use.
# webhook_source = "env" needs no extra IAM at all.
resource "aws_iam_role_policy" "webhook_secretsmanager" {
  count = var.webhook_source == "secretsmanager" ? 1 : 0
  name  = "${local.name}-read-secret"
  role  = aws_iam_role.lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "secretsmanager:GetSecretValue"
      Resource = aws_secretsmanager_secret.slack[0].arn
    }]
  })
}

resource "aws_iam_role_policy" "webhook_ssm" {
  count = var.webhook_source == "ssm" ? 1 : 0
  name  = "${local.name}-read-parameter"
  role  = aws_iam_role.lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = "ssm:GetParameter"
        Resource = "arn:aws:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter/${local.name}/slack-webhook"
      },
      # SecureString decryption via the account's default SSM key. Scoped by
      # ViaService because kms:Decrypt cannot target an alias ARN directly.
      {
        Effect   = "Allow"
        Action   = "kms:Decrypt"
        Resource = "*"
        Condition = {
          StringEquals = {
            "kms:ViaService" = "ssm.${var.region}.amazonaws.com"
          }
        }
      }
    ]
  })
}

# ----------------------------------------------------------------------- lambda
data "archive_file" "src" {
  type        = "zip"
  source_dir  = "${path.module}/../src"
  output_path = "${path.module}/build/handler.zip"
}

resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${local.name}"
  retention_in_days = 30
  tags              = var.tags
}

resource "aws_lambda_function" "monitor" {
  function_name    = local.name
  role             = aws_iam_role.lambda.arn
  handler          = "handler.lambda_handler"
  runtime          = "python3.12"
  filename         = data.archive_file.src.output_path
  source_code_hash = data.archive_file.src.output_base64sha256
  timeout          = 120
  memory_size      = 256

  environment {
    variables = merge(local.webhook_env, {
      BUCKET             = var.bucket
      PREFIX             = var.prefix
      SUFFIX             = var.suffix
      STALE_DAYS         = tostring(var.stale_days)
      BUSINESS_DAYS_ONLY = tostring(var.business_days_only)
      RENOTIFY_HOURS     = tostring(var.renotify_hours)
      MONITOR_ID         = var.monitor_id
      STATE_TABLE        = aws_dynamodb_table.state.name
      METRIC_NAMESPACE   = "CutAndDry/FeedFreshness"
      DISPLAY_TZ         = var.display_tz
    })
  }

  depends_on = [aws_cloudwatch_log_group.lambda]
  tags       = var.tags
}

# --------------------------------------------------------------------- schedule
resource "aws_cloudwatch_event_rule" "daily" {
  name                = "${local.name}-daily"
  description         = "Daily freshness check for ${var.bucket}/${var.prefix}"
  schedule_expression = var.schedule_expression
  tags                = var.tags
}

resource "aws_cloudwatch_event_target" "daily" {
  rule      = aws_cloudwatch_event_rule.daily.name
  target_id = "lambda"
  arn       = aws_lambda_function.monitor.arn
}

resource "aws_lambda_permission" "events" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.monitor.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.daily.arn
}

# ------------------------------------------------- watchdog-on-the-watchdog
# If the monitor itself breaks you get silence, which looks identical to a
# healthy feed. These two alarms cover that.
resource "aws_sns_topic" "monitor_health" {
  name = "${local.name}-health"
  tags = var.tags
}

resource "aws_cloudwatch_metric_alarm" "monitor_errors" {
  alarm_name          = "${local.name}-errors"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  metric_name         = "Errors"
  namespace           = "AWS/Lambda"
  period              = 86400
  statistic           = "Sum"
  threshold           = 1
  dimensions          = { FunctionName = aws_lambda_function.monitor.function_name }
  alarm_description   = "Feed monitor Lambda is erroring; staleness alerts may not fire."
  alarm_actions       = [aws_sns_topic.monitor_health.arn]
  treat_missing_data  = "notBreaching"
  tags                = var.tags
}

resource "aws_cloudwatch_metric_alarm" "monitor_not_running" {
  alarm_name          = "${local.name}-not-running"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 2
  metric_name         = "Invocations"
  namespace           = "AWS/Lambda"
  period              = 86400
  statistic           = "Sum"
  threshold           = 1
  dimensions          = { FunctionName = aws_lambda_function.monitor.function_name }
  alarm_description   = "Feed monitor has not run for 2 days; the schedule is broken."
  alarm_actions       = [aws_sns_topic.monitor_health.arn]
  treat_missing_data  = "breaching"
  tags                = var.tags
}

# ---------------------------------------------------------------------- outputs
output "function_name" {
  value = aws_lambda_function.monitor.function_name
}

output "webhook_secret_arn" {
  description = "Empty unless webhook_source = secretsmanager"
  value       = try(aws_secretsmanager_secret.slack[0].arn, "")
}

output "webhook_ssm_param" {
  description = "Empty unless webhook_source = ssm"
  value       = try(aws_ssm_parameter.slack[0].name, "")
}

output "health_topic_arn" {
  value = aws_sns_topic.monitor_health.arn
}
