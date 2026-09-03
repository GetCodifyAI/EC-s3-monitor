#!/usr/bin/env python3
"""
Exercise the feed-freshness monitor on your laptop, before anything is deployed.

  --offline   Stub S3 and SSM entirely. No AWS credentials, no permissions.
  (default)   Real S3 listing with your own credentials; watermark kept in a
              local JSON file instead of SSM. Needs only s3:ListBucket.

Slack is always dry-run here: the exact text is printed, never posted.

  python3 cfn/run_local.py --offline --scenario stale
  python3 cfn/run_local.py --offline --scenario mixed
  python3 cfn/run_local.py --offline --scenario recovered
  python3 cfn/run_local.py
"""
import argparse, json, os, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "src"))
STATE_FILE = HERE / ".watermark.json"

FEEDS = [
    {"id": "enterprise-cafe/purchase-orders",
     "prefix": "enterprise-cafe/prod/incoming/purchase-orders/"},
    {"id": "enterprise-cafe/invoices",
     "prefix": "enterprise-cafe/prod/incoming/invoices/"},
    {"id": "birite/catalog",
     "prefix": "birite/prod/incoming/catalog/", "stale_days": 7},
]

DEFAULTS = {
    # boto3 builds clients at import time, so a region is required even offline.
    "AWS_DEFAULT_REGION": "us-east-2",
    "BUCKET": "cut-dry-vendor-integration",
    "FEEDS": json.dumps(FEEDS),
    "STALE_DAYS": "3",
    "SUFFIX": ".csv",
    "BUSINESS_DAYS_ONLY": "true",
    "RENOTIFY_HOURS": "24",
    "MONITOR_ID": "s3-feed-freshness",
    "STATE_PARAM": "/platform-monitors/local/watermark",
    "WEBHOOK_PARAM": "/platform/slack/webhook-dam-alerts",
    "DISPLAY_TZ": "America/Los_Angeles",
    "DRY_RUN": "true",
}

# days idle per feed, by scenario
SCENARIOS = {
    "clean":     {"enterprise-cafe/purchase-orders": 1,  "enterprise-cafe/invoices": 0.5, "birite/catalog": 2},
    "stale":     {"enterprise-cafe/purchase-orders": 18, "enterprise-cafe/invoices": 9,   "birite/catalog": 20},
    "mixed":     {"enterprise-cafe/purchase-orders": 18, "enterprise-cafe/invoices": 0.5, "birite/catalog": 4},
    "recovered": {"enterprise-cafe/purchase-orders": 0.2,"enterprise-cafe/invoices": 0.5, "birite/catalog": 1},
    "empty":     {},
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--scenario", choices=sorted(SCENARIOS), default="mixed")
    ap.add_argument("--reset", action="store_true", help="clear the local watermark first")
    args = ap.parse_args()

    for k, v in DEFAULTS.items():
        os.environ.setdefault(k, v)

    if args.reset:
        STATE_FILE.write_text("{}")

    import monitor

    def load():
        return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}

    def save(state):
        STATE_FILE.write_text(json.dumps(state, indent=2))

    monitor._load_state = load
    monitor._save_state = save

    if args.offline:
        ages = SCENARIOS[args.scenario]

        def fake_scan(prefix, suffix, _ages=ages):
            fid = next((f["id"] for f in FEEDS if f["prefix"] == prefix), None)
            if fid not in _ages:
                return None, None, 0
            ts = datetime.now(timezone.utc) - timedelta(days=_ages[fid])
            name = f"{fid.split('/')[-1]}_{ts.strftime('%Y%m%d')}_587920829.csv"
            return prefix + name, ts, 198
        monitor._scan = fake_scan

    result = monitor.lambda_handler({"source": "local"}, None)
    print("\n" + "=" * 68)
    print(json.dumps(result, indent=2))
    print(f"\nwatermark -> {STATE_FILE}")


if __name__ == "__main__":
    main()
