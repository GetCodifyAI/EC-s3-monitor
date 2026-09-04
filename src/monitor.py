"""
S3 staleness monitor - platform alert.

Watches one or more prefixes in an S3 bucket. On each run it finds the newest
object under each prefix and, when nothing has landed for longer than the
configured threshold, posts one aggregated plain-text alert to Slack through an
incoming webhook. Silent on a clean run.

Deliberately stateless. There is no database, no table and no watermark: every
run recomputes the answer from S3 alone. The cost of that choice is that a stale
prefix alerts once per scheduled run for as long as it stays stale, rather than
once per outage. With a daily schedule that is one message a day, which reads as
a standing reminder that the feed is still dead. See "Design notes" in README.md
before changing this.

Read-only against S3. It calls ListObjectsV2 and nothing else, so it cannot read
a byte of object content and cannot write anything into the monitored bucket.
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
except ImportError:  # pragma: no cover - Python < 3.9
    ZoneInfo = None

LOG = logging.getLogger()
LOG.setLevel(os.environ.get("LOG_LEVEL", "INFO"))


# --------------------------------------------------------------------- config
def _env():
    """
    Read configuration from the environment at call time.

    Deliberately not module-level constants: the offline harness and the unit
    tests set os.environ and then call the handler, and module-level reads would
    freeze whatever happened to be set at import.
    """
    return {
        "bucket": os.environ["BUCKET"],
        # [{id, prefix, suffix?, stale_days?}]
        "feeds": json.loads(os.environ["FEEDS"]),
        "stale_days": int(os.environ.get("STALE_DAYS", "3")),
        "suffix": os.environ.get("SUFFIX", "").lower(),
        "business_days_only": os.environ.get("BUSINESS_DAYS_ONLY", "false").lower() == "true",
        "monitor_id": os.environ.get("MONITOR_ID", "s3-staleness-monitor"),
        "webhook_param": os.environ["WEBHOOK_PARAM"],
        "display_tz": os.environ.get("DISPLAY_TZ", "America/Los_Angeles"),
        "dry_run": os.environ.get("DRY_RUN", "true").lower() == "true",
    }


_clients = {}


def _s3():
    if "s3" not in _clients:
        _clients["s3"] = boto3.client("s3")
    return _clients["s3"]


def _ssm():
    if "ssm" not in _clients:
        _clients["ssm"] = boto3.client("ssm")
    return _clients["ssm"]


# ----------------------------------------------------------------- formatting
def _tz(name):
    if ZoneInfo is None:
        return timezone.utc
    try:
        return ZoneInfo(name)
    except Exception:
        LOG.warning("Timezone %s unavailable; falling back to UTC", name)
        return timezone.utc


def _fmt(ts, tz_name):
    """MM/DD/YY HH:MM in the display timezone."""
    if ts is None:
        return "never"
    return ts.astimezone(_tz(tz_name)).strftime("%m/%d/%y %H:%M %Z")


# ---------------------------------------------------------------- s3 scanning
def _scan(bucket, prefix, suffix):
    """
    Newest matching object under prefix.

    Returns (key, last_modified, count). Directory placeholder keys and
    zero-byte objects are skipped: an empty marker landing in the prefix should
    not reset the staleness clock.
    """
    paginator = _s3().get_paginator("list_objects_v2")
    newest_key, newest_ts, count = None, None, 0
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
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


def _elapsed_days(last_ts, now, business_days_only, tz_name):
    """
    Days since last_ts.

    None means nothing has ever landed, which is stale past any finite
    threshold, so it returns infinity rather than a large number. When
    business_days_only is set, only Mon-Fri are counted, so a Friday delivery
    does not read as stale on Monday morning.
    """
    if last_ts is None:
        return float("inf")
    if not business_days_only:
        return (now - last_ts).total_seconds() / 86400.0
    tz = _tz(tz_name)
    day = last_ts.astimezone(tz).date() + timedelta(days=1)
    today = now.astimezone(tz).date()
    business = 0
    while day <= today:
        if day.weekday() < 5:
            business += 1
        day += timedelta(days=1)
    return float(business)


# ------------------------------------------------------------------ slack post
_webhook_cache = {}


def _webhook(param_name):
    """
    Resolve the webhook URL from SSM, once per warm container.

    Accepts either a bare URL or a JSON object with a webhook_url key, because
    both shapes turn up in SSM depending on who created the parameter. Anything
    that is not a Slack webhook is rejected loudly rather than POSTed to.
    """
    if param_name not in _webhook_cache:
        raw = _ssm().get_parameter(Name=param_name, WithDecryption=True)["Parameter"]["Value"]
        try:
            url = json.loads(raw)["webhook_url"]
        except (json.JSONDecodeError, KeyError, TypeError):
            url = raw.strip()
        if not url.startswith("https://hooks.slack.com/"):
            raise RuntimeError(
                f"Value at {param_name} is not a Slack webhook URL"
            )
        _webhook_cache[param_name] = url
    return _webhook_cache[param_name]


def _post(text, cfg):
    """
    Plain-text post to Slack.

    Raises on failure. That is deliberate: an unhandled exception increments the
    Lambda Errors metric and trips the alarm. A monitor that swallows its own
    delivery failures is indistinguishable from a healthy feed.
    """
    if cfg["dry_run"]:
        LOG.info("DRY_RUN, would post to Slack:\n%s", text)
        return
    payload = json.dumps({"text": text}).encode()
    req = urllib.request.Request(
        _webhook(cfg["webhook_param"]),
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


def _check_webhook(cfg):
    """
    Dry-run credential check.

    Resolving the webhook parameter is the one step a clean dry run would
    otherwise skip entirely, and it is the assumption most likely to be wrong:
    a mistyped path, a missing ssm:GetParameter grant, or a parameter holding
    something that is not a webhook. Every dry run resolves it and reports the
    result. Logs the host only - never the URL, which is itself a secret.
    """
    try:
        host = urllib.parse.urlsplit(_webhook(cfg["webhook_param"])).netloc
    except Exception as exc:  # noqa: BLE001 - reported in the summary, never raised
        LOG.error("Webhook check FAILED for %s: %s", cfg["webhook_param"], exc)
        return {"parameter": cfg["webhook_param"], "ok": False, "detail": str(exc)[:200]}
    LOG.info("Webhook check OK: %s resolves to %s", cfg["webhook_param"], host)
    return {"parameter": cfg["webhook_param"], "ok": True, "detail": host}


# ------------------------------------------------------------------ message
def _unit(cfg, n=None):
    """Plural by default; singular only when n is exactly 1."""
    base = "business day" if cfg["business_days_only"] else "day"
    return base if n == 1 else base + "s"


def _stale_text(stale, total, overall_ts, cfg):
    tz_name = cfg["display_tz"]
    head = ":rotating_light: S3 staleness alert"
    if total > 1:
        head += f" - {len(stale):,} of {total:,} prefixes stale"
    lines = [head, ""]
    for f in stale:
        threshold = f"threshold {f['threshold']:,} {_unit(cfg, f['threshold'])}"
        lines.append(f"*{f['id']}*  `s3://{cfg['bucket']}/{f['prefix']}`")
        if f["elapsed"] == float("inf"):
            lines.append(f"    no file has ever landed here  ({threshold})")
        else:
            lines.append(
                f"    last file {_fmt(f['last_ts'], tz_name)}  -  "
                f"idle {f['elapsed']:,.1f} {_unit(cfg)}  ({threshold})"
            )
        if f["last_key"]:
            lines.append(f"    newest object: `{f['last_key'].rsplit('/', 1)[-1]}`")
        noun = "file" if f["count"] == 1 else "files"
        lines.append(f"    {f['count']:,} {noun} in prefix")
        lines.append("")
    if total > 1:
        lines.append(f"Newest file across all prefixes: {_fmt(overall_ts, tz_name)}")
    lines.append(f"Bucket `{cfg['bucket']}`  -  monitor `{cfg['monitor_id']}`")
    return "\n".join(lines)


# ------------------------------------------------------------------- handler
def lambda_handler(event, context):
    cfg = _env()
    now = datetime.now(timezone.utc)
    results, stale = [], []
    overall_ts = None

    for feed in cfg["feeds"]:
        fid = feed["id"]
        prefix = feed["prefix"]
        suffix = feed.get("suffix", cfg["suffix"]).lower()
        threshold = int(feed.get("stale_days", cfg["stale_days"]))

        last_key, last_ts, count = _scan(cfg["bucket"], prefix, suffix)
        elapsed = _elapsed_days(last_ts, now, cfg["business_days_only"], cfg["display_tz"])
        is_stale = elapsed >= threshold

        if last_ts and (overall_ts is None or last_ts > overall_ts):
            overall_ts = last_ts

        if is_stale:
            stale.append({
                "id": fid, "prefix": prefix, "last_key": last_key,
                "last_ts": last_ts, "elapsed": elapsed,
                "threshold": threshold, "count": count,
            })

        results.append({
            "feed_id": fid,
            "prefix": prefix,
            "objects": count,
            "last_file_key": last_key,
            "last_file_at": last_ts.isoformat() if last_ts else None,
            "elapsed_days": None if elapsed == float("inf") else round(elapsed, 2),
            "threshold_days": threshold,
            "status": "STALE" if is_stale else "OK",
        })

    posted = []
    if stale:
        _post(_stale_text(stale, len(cfg["feeds"]), overall_ts, cfg), cfg)
        posted.append("stale")

    summary = {
        "monitor_id": cfg["monitor_id"],
        "bucket": cfg["bucket"],
        "dry_run": cfg["dry_run"],
        "webhook_check": _check_webhook(cfg) if cfg["dry_run"] else None,
        "prefixes_checked": len(cfg["feeds"]),
        "prefixes_stale": len(stale),
        "newest_file_overall": overall_ts.isoformat() if overall_ts else None,
        "slack_posts": posted,
        "feeds": results,
    }
    LOG.info(json.dumps(summary))
    return summary
