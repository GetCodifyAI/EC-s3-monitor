"""
S3 feed staleness monitor.

Runs on a daily schedule. Finds the newest object under a monitored S3 prefix
and posts to Slack when nothing new has landed for STALE_DAYS consecutive days.
Also posts a recovery message when the feed starts flowing again.

State (current OK/STALE status + last notification time) lives in DynamoDB so
that the alert fires on transition instead of every single run.
"""

import json
import logging
import os
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import boto3
from botocore.exceptions import ClientError

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

LOG = logging.getLogger()
LOG.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# ---------------------------------------------------------------- configuration
BUCKET = os.environ["BUCKET"]                                  # cut-dry-vendor-integration
PREFIX = os.environ["PREFIX"]                                  # enterprise-cafe/prod/incoming/purchase-orders/
SUFFIX = os.environ.get("SUFFIX", ".csv").lower()              # "" disables suffix filtering
STALE_DAYS = int(os.environ.get("STALE_DAYS", "3"))
BUSINESS_DAYS_ONLY = os.environ.get("BUSINESS_DAYS_ONLY", "false").lower() == "true"
RENOTIFY_HOURS = int(os.environ.get("RENOTIFY_HOURS", "24"))   # 0 = alert once per outage
MONITOR_ID = os.environ.get("MONITOR_ID", "enterprise-cafe-po-prod")
STATE_TABLE = os.environ["STATE_TABLE"]

# Slack credential lookup, in priority order. The first one set wins.
#
#   Bot token (preferred) - posts via chat.postMessage to SLACK_CHANNEL, so the
#   target channel is configuration rather than a property of the credential.
#     SLACK_BOT_TOKEN_SSM   SSM Parameter Store SecureString holding xoxb-...
#     SLACK_BOT_TOKEN       plain env var (last resort)
#
#   Incoming webhook (fallback) - bound at creation to exactly one channel.
#     SLACK_SECRET_ARN      Secrets Manager
#     SLACK_SSM_PARAM       SSM Parameter Store SecureString
#     SLACK_WEBHOOK_URL     plain env var (last resort - readable by anyone with
#                           lambda:GetFunctionConfiguration, and lands in TF state)
SLACK_BOT_TOKEN_SSM = os.environ.get("SLACK_BOT_TOKEN_SSM")
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
SLACK_CHANNEL = os.environ.get("SLACK_CHANNEL", "")            # e.g. C04F7EJU5PB
SLACK_SECRET_ARN = os.environ.get("SLACK_SECRET_ARN")
SLACK_SSM_PARAM = os.environ.get("SLACK_SSM_PARAM")
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL")
METRIC_NAMESPACE = os.environ.get("METRIC_NAMESPACE", "CutAndDry/FeedFreshness")
DISPLAY_TZ_NAME = os.environ.get("DISPLAY_TZ", "America/Los_Angeles")
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"
APP_NAME_HINT = os.environ.get("SLACK_APP_NAME", "S3 Feed Monitor")

s3 = boto3.client("s3")
ddb = boto3.client("dynamodb")
cw = boto3.client("cloudwatch")

_credential_cache = None    # ("bot", token) | ("webhook", url)


# ------------------------------------------------------------------- formatting
def _display_tz():
    if ZoneInfo is None:
        return timezone.utc
    try:
        return ZoneInfo(DISPLAY_TZ_NAME)
    except Exception:  # tzdata missing in the runtime image
        LOG.warning("Timezone %s unavailable; displaying times in UTC", DISPLAY_TZ_NAME)
        return timezone.utc


def _fmt(ts):
    """MM/DD/YY HH:MM in the display timezone."""
    if ts is None:
        return "never"
    local = ts.astimezone(_display_tz())
    return local.strftime("%m/%d/%y %H:%M %Z")


# ----------------------------------------------------------------- s3 inspection
def _latest_object():
    """Return (key, last_modified, object_count) for the newest matching object."""
    paginator = s3.get_paginator("list_objects_v2")
    newest_key, newest_ts, count = None, None, 0

    for page in paginator.paginate(Bucket=BUCKET, Prefix=PREFIX):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith("/") or obj["Size"] == 0:
                continue                                  # folder placeholder
            if SUFFIX and not key.lower().endswith(SUFFIX):
                continue
            count += 1
            if newest_ts is None or obj["LastModified"] > newest_ts:
                newest_key, newest_ts = key, obj["LastModified"]

    return newest_key, newest_ts, count


def _elapsed_days(last_ts, now):
    """Days since last_ts. Counts Mon-Fri only when BUSINESS_DAYS_ONLY is set."""
    if last_ts is None:
        return float("inf")
    if not BUSINESS_DAYS_ONLY:
        return (now - last_ts).total_seconds() / 86400.0

    tz = _display_tz()
    day = last_ts.astimezone(tz).date() + timedelta(days=1)
    today = now.astimezone(tz).date()
    business = 0
    while day <= today:
        if day.weekday() < 5:
            business += 1
        day += timedelta(days=1)
    return float(business)


# ---------------------------------------------------------------------- ddb state
def _get_state():
    try:
        item = ddb.get_item(
            TableName=STATE_TABLE,
            Key={"monitor_id": {"S": MONITOR_ID}},
            ConsistentRead=True,
        ).get("Item")
    except ClientError:
        LOG.exception("Could not read monitor state; assuming first run")
        return {}
    if not item:
        return {}
    return {
        "status": item.get("status", {}).get("S", "OK"),
        "last_notified_at": item.get("last_notified_at", {}).get("S"),
    }


def _put_state(status, last_notified_at, last_file_key, last_file_at):
    item = {
        "monitor_id": {"S": MONITOR_ID},
        "status": {"S": status},
        "checked_at": {"S": datetime.now(timezone.utc).isoformat()},
    }
    if last_notified_at:
        item["last_notified_at"] = {"S": last_notified_at}
    if last_file_key:
        item["last_file_key"] = {"S": last_file_key}
    if last_file_at:
        item["last_file_at"] = {"S": last_file_at.isoformat()}
    ddb.put_item(TableName=STATE_TABLE, Item=item)


# -------------------------------------------------------------------- slack post
def _ssm_value(name):
    return boto3.client("ssm").get_parameter(Name=name, WithDecryption=True)[
        "Parameter"
    ]["Value"]


def _unwrap(raw, *keys):
    """Accept either a bare string or a JSON blob with one of `keys`."""
    try:
        doc = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw.strip()
    if isinstance(doc, dict):
        for key in keys:
            if key in doc:
                return str(doc[key]).strip()
    return raw.strip()


def _credential():
    """Resolve the Slack credential once per container.

    Returns ("bot", token) or ("webhook", url).
    """
    global _credential_cache
    if _credential_cache is not None:
        return _credential_cache

    if SLACK_BOT_TOKEN_SSM:
        token = _unwrap(_ssm_value(SLACK_BOT_TOKEN_SSM), "bot_token", "token")
        _credential_cache = ("bot", token)
    elif SLACK_BOT_TOKEN:
        _credential_cache = ("bot", SLACK_BOT_TOKEN.strip())
    elif SLACK_SECRET_ARN:
        raw = boto3.client("secretsmanager").get_secret_value(
            SecretId=SLACK_SECRET_ARN
        )["SecretString"]
        _credential_cache = ("webhook", _unwrap(raw, "webhook_url"))
    elif SLACK_SSM_PARAM:
        _credential_cache = ("webhook", _unwrap(_ssm_value(SLACK_SSM_PARAM), "webhook_url"))
    elif SLACK_WEBHOOK_URL:
        _credential_cache = ("webhook", SLACK_WEBHOOK_URL.strip())
    else:
        raise RuntimeError(
            "No Slack credential configured. Set SLACK_BOT_TOKEN_SSM (preferred) "
            "or one of SLACK_BOT_TOKEN, SLACK_SECRET_ARN, SLACK_SSM_PARAM, "
            "SLACK_WEBHOOK_URL."
        )

    kind, value = _credential_cache
    if kind == "bot":
        if not value.startswith(("xoxb-", "xoxp-")):
            _credential_cache = None
            raise RuntimeError(
                "Resolved Slack bot token does not start with xoxb-. Check that the "
                "SSM parameter holds the Bot User OAuth Token, not the signing secret."
            )
        if not SLACK_CHANNEL:
            _credential_cache = None
            raise RuntimeError("SLACK_CHANNEL must be set when using a bot token.")
    elif not value.startswith("https://hooks.slack.com/"):
        _credential_cache = None
        raise RuntimeError("Resolved webhook does not look like a Slack webhook URL")

    return _credential_cache


def _http_post(url, payload, headers):
    req = urllib.request.Request(
        url, data=payload, headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def _post_slack(blocks, fallback):
    """Post one message. Raises on failure so the Lambda errors and alarms fire."""
    # Resolve the credential lazily: a DRY_RUN must be able to render the exact
    # payload with no Slack secret and no ssm:GetParameter permission at all.
    if DRY_RUN:
        kind, credential = ("bot" if (SLACK_BOT_TOKEN_SSM or SLACK_BOT_TOKEN or SLACK_CHANNEL)
                            else "webhook"), "DRY_RUN"
    else:
        kind, credential = _credential()

    if kind == "bot":
        body = {
            "channel": SLACK_CHANNEL,
            "text": fallback,          # notification + accessibility fallback
            "blocks": blocks,
            "unfurl_links": False,
            "unfurl_media": False,
        }
        url = "https://slack.com/api/chat.postMessage"
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {credential}",
        }
    else:
        body = {"text": fallback, "blocks": blocks}
        url = credential
        headers = {"Content-Type": "application/json"}

    payload = json.dumps(body).encode()

    if DRY_RUN:
        LOG.info("DRY_RUN, would post to Slack via %s: %s", kind, payload.decode())
        return

    status, text = _http_post(url, payload, headers)

    if kind == "bot":
        try:
            doc = json.loads(text)
        except json.JSONDecodeError:
            raise RuntimeError(f"Slack returned HTTP {status}: {text[:400]}")
        if not doc.get("ok"):
            err = doc.get("error", "unknown_error")
            hint = {
                "not_in_channel": (
                    f" - invite the app to the channel: /invite @{APP_NAME_HINT}"
                ),
                "channel_not_found": " - check SLACK_CHANNEL is the channel ID, not the name",
                "invalid_auth": " - the bot token is wrong, revoked, or from another workspace",
                "missing_scope": f" - the app needs the chat:write scope (needed: {doc.get('needed')})",
                "ratelimited": " - backed off by Slack; the next scheduled run will retry",
            }.get(err, "")
            raise RuntimeError(f"Slack chat.postMessage failed: {err}{hint}")
        LOG.info("Posted to Slack channel %s at ts %s", SLACK_CHANNEL, doc.get("ts"))
        return

    if status != 200 or text.strip() != "ok":
        raise RuntimeError(f"Slack webhook returned {status}: {text[:400]}")


def _stale_blocks(last_key, last_ts, elapsed, object_count):
    unit = "business days" if BUSINESS_DAYS_ONLY else "days"
    elapsed_txt = "no files ever" if elapsed == float("inf") else f"{elapsed:,.1f} {unit}"
    fields = [
        {"type": "mrkdwn", "text": f"*Last file*\n{_fmt(last_ts)}"},
        {"type": "mrkdwn", "text": f"*Idle for*\n{elapsed_txt}"},
        {"type": "mrkdwn", "text": f"*Threshold*\n{STALE_DAYS} {unit}"},
        {"type": "mrkdwn", "text": f"*Files in prefix*\n{object_count:,}"},
    ]
    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": ":rotating_light: PO feed has gone quiet"},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"No new file in `s3://{BUCKET}/{PREFIX}`"},
            "fields": fields,
        },
    ]
    if last_key:
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"Most recent object: `{last_key}`"}],
            }
        )
    blocks.append(
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"Monitor: `{MONITOR_ID}`"}],
        }
    )
    return blocks, f"No new PO file in s3://{BUCKET}/{PREFIX} for {elapsed_txt}"


def _recovery_blocks(last_key, last_ts):
    return (
        [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f":white_check_mark: *PO feed is flowing again* — "
                        f"new file in `s3://{BUCKET}/{PREFIX}`"
                    ),
                },
                "fields": [
                    {"type": "mrkdwn", "text": f"*Arrived*\n{_fmt(last_ts)}"},
                    {"type": "mrkdwn", "text": f"*Object*\n`{last_key}`"},
                ],
            }
        ],
        f"PO feed recovered: new file at {_fmt(last_ts)}",
    )


# ------------------------------------------------------------------------ metrics
def _emit_metric(elapsed, object_count):
    hours = 9_999.0 if elapsed == float("inf") else elapsed * 24.0
    try:
        cw.put_metric_data(
            Namespace=METRIC_NAMESPACE,
            MetricData=[
                {
                    "MetricName": "HoursSinceLastFile",
                    "Dimensions": [{"Name": "MonitorId", "Value": MONITOR_ID}],
                    "Value": hours,
                    "Unit": "None",
                },
                {
                    "MetricName": "ObjectsInPrefix",
                    "Dimensions": [{"Name": "MonitorId", "Value": MONITOR_ID}],
                    "Value": float(object_count),
                    "Unit": "Count",
                },
            ],
        )
    except ClientError:
        LOG.exception("Failed to publish CloudWatch metrics (non-fatal)")


# ------------------------------------------------------------------------ handler
def lambda_handler(event, context):
    now = datetime.now(timezone.utc)
    last_key, last_ts, object_count = _latest_object()
    elapsed = _elapsed_days(last_ts, now)
    is_stale = elapsed >= STALE_DAYS

    _emit_metric(elapsed, object_count)

    prior = _get_state()
    prior_status = prior.get("status", "OK")
    last_notified_at = prior.get("last_notified_at")
    notified = False

    if is_stale:
        due = True
        if prior_status == "STALE":
            if RENOTIFY_HOURS <= 0:
                due = False
            elif last_notified_at:
                age = (now - datetime.fromisoformat(last_notified_at)).total_seconds() / 3600
                due = age >= RENOTIFY_HOURS
        if due:
            blocks, fallback = _stale_blocks(last_key, last_ts, elapsed, object_count)
            _post_slack(blocks, fallback)
            last_notified_at, notified = now.isoformat(), True
        status = "STALE"
    else:
        if prior_status == "STALE":
            blocks, fallback = _recovery_blocks(last_key, last_ts)
            _post_slack(blocks, fallback)
            last_notified_at, notified = now.isoformat(), True
        status = "OK"

    _put_state(status, last_notified_at, last_key, last_ts)

    result = {
        "monitor_id": MONITOR_ID,
        "bucket": BUCKET,
        "prefix": PREFIX,
        "objects_in_prefix": object_count,
        "last_file_key": last_key,
        "last_file_at": last_ts.isoformat() if last_ts else None,
        "elapsed_days": None if elapsed == float("inf") else round(elapsed, 2),
        "threshold_days": STALE_DAYS,
        "prior_status": prior_status,
        "status": status,
        "slack_notified": notified,
    }
    LOG.info(json.dumps(result))
    return result
