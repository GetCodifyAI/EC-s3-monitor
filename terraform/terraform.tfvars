# Count Mon-Fri only, so a Friday drop does not alert on Monday morning.
business_days_only = true

# Post to #dam-alerts with a bot token. The token itself is loaded into the
# SSM SecureString out of band (see README) and never enters Terraform state.
slack_source     = "bot_token_ssm"
slack_channel_id = "C04F7EJU5PB" # #dam-alerts

tags = {
  Project = "s3-feed-monitor"
  Owner   = "data-eng"
  Feed    = "enterprise-cafe-po"
}
