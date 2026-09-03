#!/usr/bin/env python3
"""
Run the monitor logic on your laptop, before anything is deployed.

Two modes:

  --offline   No AWS at all. Stubs S3 and DynamoDB so you can prove the
              staleness maths and the Slack block layout with zero permissions.

  (default)   Real S3 ListObjectsV2 with your own credentials, but DynamoDB
              state is kept in local/.feed-monitor-state.json instead of the
              table. Needs only s3:ListBucket on the monitored prefix.

Slack is DRY_RUN by default: the payload is printed, not posted. Pass --post
with SLACK_BOT_TOKEN set to actually deliver a message.

    python3 local/run_local.py --offline --days-since 5
    python3 local/run_local.py
    SLACK_BOT_TOKEN=xoxb-... python3 local/run_local.py --post
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

STATE_FILE = Path(__file__).resolve().parent / ".feed-monitor-state.json"

DEFAULTS = {
    # boto3 constructs its clients at import time, so a region must exist even
    # in --offline mode where no call is ever made.
    "AWS_DEFAULT_REGION": "us-east-2",
    "BUCKET": "cut-dry-vendor-integration",
    "PREFIX": "enterprise-cafe/prod/incoming/purchase-orders/",
    "SUFFIX": ".csv",
    "STALE_DAYS": "3",
    "BUSINESS_DAYS_ONLY": "true",
    "RENOTIFY_HOURS": "24",
    "MONITOR_ID": "enterprise-cafe-po-prod",
    "STATE_TABLE": "local-json-file",
    "SLACK_CHANNEL": "C04F7EJU5PB",
    "DISPLAY_TZ": "America/Los_Angeles",
    "DRY_RUN": "true",
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true",
                    help="stub S3 entirely; no AWS credentials needed")
    ap.add_argument("--days-since", type=float, default=5.0,
                    help="offline mode: how old to pretend the newest file is")
    ap.add_argument("--empty", action="store_true",
                    help="offline mode: pretend the prefix has no files at all")
    ap.add_argument("--post", action="store_true",
                    help="really post to Slack (needs SLACK_BOT_TOKEN)")
    ap.add_argument("--stale-days", type=int)
    args = ap.parse_args()

    for k, v in DEFAULTS.items():
        os.environ.setdefault(k, v)
    if args.stale_days is not None:
        os.environ["STALE_DAYS"] = str(args.stale_days)
    if args.post:
        os.environ["DRY_RUN"] = "false"
        if not os.environ.get("SLACK_BOT_TOKEN"):
            sys.exit("--post needs SLACK_BOT_TOKEN=xoxb-... in the environment")

    import handler  # imported after env is set; module reads env at import time

    # ---- local JSON state instead of DynamoDB ----------------------------
    def get_state():
        if not STATE_FILE.exists():
            return {}
        return json.loads(STATE_FILE.read_text())

    def put_state(status, last_notified_at, last_file_key, last_file_at):
        STATE_FILE.write_text(json.dumps({
            "status": status,
            "last_notified_at": last_notified_at,
            "last_file_key": last_file_key,
            "last_file_at": last_file_at.isoformat() if last_file_at else None,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }, indent=2))

    handler._get_state = get_state
    handler._put_state = put_state
    handler._emit_metric = lambda *a, **k: None

    if args.offline:
        if args.empty:
            handler._latest_object = lambda: (None, None, 0)
        else:
            ts = datetime.now(timezone.utc) - timedelta(days=args.days_since)
            handler._latest_object = lambda: (
                f"{os.environ['PREFIX']}141_PO Data_20260818_587920829.csv", ts, 198)

    result = handler.lambda_handler({"source": "local"}, None)
    print(json.dumps(result, indent=2))
    print(f"\nstate -> {STATE_FILE}")


if __name__ == "__main__":
    main()
