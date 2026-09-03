"""
S3 feed freshness monitor  -  platform alert.

Runs daily on an EventBridge schedule. For each configured vendor feed it finds
the newest object under that prefix and, when nothing has landed for longer than
the feed's threshold, posts one aggregated plain-text alert to Slack through the
platform incoming webhook. Silent on a clean run.

State is a single SSM parameter holding a small JSON map, so the monitor alerts
on transition rather than every day, and can report recovery. There is no
database and no table to manage.

Deliberately read-only against S3. It never writes to the monitored bucket -
doing so would fire that bucket's ObjectCreated notification into the Snowpipe
SNS topic.
"""

import json
import logging
import os
import urllib.error
import urllib.parse
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

# ------------------------------------------------------------------- config
BUCKET = os.environ["BUCKET"]
FEEDS = json.loads(os.environ["FEEDS"])          # [{id, prefix, suffix?, stale_days?}]
DEFAULT_STALE_DAYS = int(os.environ.get("STALE_DAYS", "3"))
DEFAULT_SUFFIX = os.environ.get("SUFFIX", ".csv").lower()
BUSINESS_DAYS_ONLY = os.environ.get("BUSINESS_DAYS_ONLY", "true").lower() == "true"
RENOTIFY_HOURS = int(os.environ.get("RENOTIFY_HOURS", "24"))
MONITOR_ID = os.environ.get("MONITOR_ID", "s3-feed-freshness")
STATE_PARAM = os.environ["STATE_PARAM"]          # SSM String holding the watermark
WEBHOOK_PARAM = os.environ["WEBHOOK_PARAM"]      # SSM SecureString holding the webhook
DISPLAY_TZ_NAME = os.environ.get("DISPLAY_TZ", "America/Los_Angeles")
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"

s3 = boto3.client("s3")
ssm = boto3.client("ssm")

_webhook_cache = None


# --------------------------------------------------------------- formatting
def _tz():
    if ZoneInfo is None:
        return timezone.utc
    try:
        return ZoneInfo(DISPLAY_TZ_NAME)
    except Exception:
        LOG.warning("Timezone %s unavailable; using UTC", DISPLAY_TZ_NAME)
        return timezone.utc


def _fmt(ts):
    """MM/DD/YY HH:MM in the display timezone."""
    if ts is None:
        return "never"
    return ts.astimezone(_tz()).strftime("%m/%d/%y %H:%M %Z")


# -------------------------------------------------------------- s3 scanning
def _scan(prefix, suffix):
    """Newest matching object under prefix. Returns (key, last_modified, count)."""
    paginator = s3.get_paginator("list_objects_v2")
    newest_key, newest_ts, count = None, None, 0
    for page in paginator.paginate(Bucket=BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith("/") or obj["Size"] == 0:
                continue
            if suffix and not key.lower().endswith(suffix):
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
    tz = _tz()
    day = last_ts.astimezone(tz).date() + timedelta(days=1)
    today = now.astimezone(tz).date()
    business = 0
    while day <= today:
        if day.weekday() < 5:
            business += 1
        day += timedelta(days=1)
    return float(business)


# ------------------------------------------------------------ ssm watermark
def _load_state():
    """{feed_id: {status, last_notified_at}}. Missing parameter = first run."""
    try:
        raw = ssm.get_parameter(Name=STATE_PARAM)["Parameter"]["Value"]
        return json.loads(raw)
    except ssm.exceptions.ParameterNotFound:
        LOG.info("No watermark yet at %s; treating as first run", STATE_PARAM)
        return {}
    except (ClientError, json.JSONDecodeError):
        LOG.exception("Could not read watermark; treating as first run")
        return {}


def _save_state(state):
    if DRY_RUN:
        LOG.info("DRY_RUN, would write watermark: %s", json.dumps(state))
        return
    try:
        ssm.put_parameter(
            Name=STATE_PARAM,
            Value=json.dumps(state, separators=(",", ":")),
            Type="String",
            Overwrite=True,
        )
    except ClientError:
        LOG.exception("Failed to persist watermark; next run may re-alert")


# -------------------------------------------------------------- slack post
def _webhook():
    global _webhook_cache
    if _webhook_cache is None:
        raw = ssm.get_parameter(Name=WEBHOOK_PARAM, WithDecryption=True)[
            "Parameter"
        ]["Value"]
        try:
            _webhook_cache = json.loads(raw)["webhook_url"]
        except (json.JSONDecodeError, KeyError, TypeError):
            _webhook_cache = raw.strip()
        if not _webhook_cache.startswith("https://hooks.slack.com/"):
            _webhook_cache = None
            raise RuntimeError("Resolved value is not a Slack webhook URL")
    return _webhook_cache


def _post(text):
    """Plain-text post. Raises on failure so the Lambda errors and the alarm trips."""
    payload = json.dumps({"text": text}).encode()
    if DRY_RUN:
        LOG.info("DRY_RUN, would post to Slack:\n%s", text)
        return
    req = urllib.request.Request(
        _webhook(),
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode()
        if body.strip() != "ok":
            raise RuntimeError(f"Unexpected Slack response: {body}")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"Slack webhook returned {exc.code}: {exc.read().decode()[:300]}"
        ) from exc


def _check_webhook():
    """
    Dry-run credential check. Resolves the webhook parameter without posting, so
    a wrong parameter path, a missing ssm:GetParameter or a value that is not a
    Slack webhook surfaces during the dry run instead of the moment the monitor
    goes live. Never logs the URL itself - only its host.
    """
    try:
        host = urllib.parse.urlsplit(_webhook()).netloc
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        LOG.error("Webhook check FAILED for %s: %s", WEBHOOK_PARAM, exc)
        return {"parameter": WEBHOOK_PARAM, "ok": False, "detail": str(exc)[:200]}
    LOG.info("Webhook check OK: %s resolves to %s", WEBHOOK_PARAM, host)
    return {"parameter": WEBHOOK_PARAM, "ok": True, "detail": host}


# ------------------------------------------------------------- message text
def _unit():
    return "business days" if BUSINESS_DAYS_ONLY else "days"


def _stale_text(stale, total_feeds, overall_ts):
    lines = [
        f":rotating_light: S3 feed staleness - {len(stale)} of {total_feeds:,} feeds stale",
        "",
    ]
    for f in stale:
        idle = "no files ever" if f["elapsed"] == float("inf") else f"{f['elapsed']:,.1f} {_unit()}"
        lines.append(f"*{f['id']}*  `{f['prefix']}`")
        lines.append(
            f"    last file {_fmt(f['last_ts'])}  -  idle {idle}  "
            f"(threshold {f['threshold']:,} {_unit()})"
        )
        if f["last_key"]:
            lines.append(f"    newest object: `{f['last_key'].rsplit('/', 1)[-1]}`")
        lines.append(f"    {f['count']:,} files in prefix")
        lines.append("")
    lines.append(f"Newest file across all feeds: {_fmt(overall_ts)}")
    lines.append(f"Bucket `{BUCKET}`  -  monitor `{MONITOR_ID}`")
    return "\n".join(lines)


def _recovery_text(recovered):
    lines = [f":white_check_mark: S3 feed recovered - {len(recovered):,} feed(s) flowing again", ""]
    for f in recovered:
        lines.append(f"*{f['id']}*  new file {_fmt(f['last_ts'])}")
        if f["last_key"]:
            lines.append(f"    `{f['last_key'].rsplit('/', 1)[-1]}`")
    lines.append("")
    lines.append(f"Bucket `{BUCKET}`  -  monitor `{MONITOR_ID}`")
    return "\n".join(lines)


# ----------------------------------------------------------------- handler
def lambda_handler(event, context):
    now = datetime.now(timezone.utc)
    state = _load_state()
    new_state = {}
    results, stale, recovered = [], [], []
    overall_ts = None

    for feed in FEEDS:
        fid = feed["id"]
        prefix = feed["prefix"]
        suffix = feed.get("suffix", DEFAULT_SUFFIX).lower()
        threshold = int(feed.get("stale_days", DEFAULT_STALE_DAYS))

        last_key, last_ts, count = _scan(prefix, suffix)
        elapsed = _elapsed_days(last_ts, now)
        is_stale = elapsed >= threshold

        if last_ts and (overall_ts is None or last_ts > overall_ts):
            overall_ts = last_ts

        prior = state.get(fid, {})
        prior_status = prior.get("status", "OK")
        last_notified_at = prior.get("last_notified_at")

        record = {
            "id": fid, "prefix": prefix, "last_key": last_key, "last_ts": last_ts,
            "elapsed": elapsed, "threshold": threshold, "count": count,
        }

        if is_stale:
            due = True
            if prior_status == "STALE":
                if RENOTIFY_HOURS <= 0:
                    due = False
                elif last_notified_at:
                    age = (now - datetime.fromisoformat(last_notified_at)).total_seconds() / 3600
                    due = age >= RENOTIFY_HOURS
            if due:
                stale.append(record)
                last_notified_at = now.isoformat()
            status = "STALE"
        else:
            if prior_status == "STALE":
                recovered.append(record)
                last_notified_at = now.isoformat()
            status = "OK"

        entry = {"status": status}
        if last_notified_at:
            entry["last_notified_at"] = last_notified_at
        new_state[fid] = entry

        results.append({
            "feed_id": fid,
            "prefix": prefix,
            "objects": count,
            "last_file_key": last_key,
            "last_file_at": last_ts.isoformat() if last_ts else None,
            "elapsed_days": None if elapsed == float("inf") else round(elapsed, 2),
            "threshold_days": threshold,
            "prior_status": prior_status,
            "status": status,
        })

    posted = []
    if stale:
        _post(_stale_text(stale, len(FEEDS), overall_ts))
        posted.append("stale")
    if recovered:
        _post(_recovery_text(recovered))
        posted.append("recovery")

    _save_state(new_state)

    # A clean dry run must still prove the webhook is reachable, otherwise the
    # only thing it proves is that nothing was stale.
    webhook_check = _check_webhook() if DRY_RUN else None

    summary = {
        "monitor_id": MONITOR_ID,
        "bucket": BUCKET,
        "dry_run": DRY_RUN,
        "webhook_check": webhook_check,
        "feeds_checked": len(FEEDS),
        "feeds_stale": sum(1 for r in results if r["status"] == "STALE"),
        "newest_file_overall": overall_ts.isoformat() if overall_ts else None,
        "slack_posts": posted,
        "feeds": results,
    }
    LOG.info(json.dumps(summary))
    return summary
