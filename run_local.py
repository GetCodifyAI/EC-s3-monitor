#!/usr/bin/env python3
"""
Offline harness. Runs the real handler against a fake S3 with no AWS access.

    python3 run_local.py                 # every scenario
    python3 run_local.py --scenario stale
    python3 run_local.py --list

This is the first thing to run and the cheapest thing to trust: it proves the
staleness logic and the exact Slack wording without credentials, without network
and without touching a bucket.
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

import stubs  # noqa: E402

stubs.install()

import monitor  # noqa: E402

WEBHOOK_PARAM = "/platform-monitors/s3-staleness/slack-test-webhook"
NOW = datetime.now(timezone.utc)


def _o(key, days_ago, size=1024):
    return stubs.obj(key, days_ago, size, now=NOW)


SCENARIOS = {
    "fresh": (
        "A file landed 4 hours ago. Healthy - the run must be silent.",
        [_o("test-dev/orders_20260904.csv", 0.17),
         _o("test-dev/orders_20260903.csv", 1.2)],
    ),
    "stale": (
        "Newest file is 5 days old. Past the 3-day threshold - alerts.",
        [_o("test-dev/orders_20260830.csv", 5.4),
         _o("test-dev/orders_20260829.csv", 6.4)],
    ),
    "empty": (
        "Nothing under the prefix at all. Reads as 'never', which is past any "
        "threshold - alerts.",
        [],
    ),
    "boundary": (
        "Exactly 3.0 days old. The threshold is >=, so this alerts. This is the "
        "case where an off-by-one would hide for weeks.",
        [_o("test-dev/orders_20260901.csv", 3.0)],
    ),
    "just-under": (
        "2.9 days old. Must stay silent - the other half of the boundary.",
        [_o("test-dev/orders_20260901.csv", 2.9)],
    ),
    "junk-only": (
        "A fresh zero-byte marker and a fresh folder key over a 9-day-old real "
        "file. Neither should reset the clock, so this alerts.",
        [_o("test-dev/", 0.01, size=0),
         _o("test-dev/_SUCCESS", 0.01, size=0),
         _o("test-dev/orders_20260826.csv", 9.1)],
    ),
}


def run(name, verbose=False):
    description, objects = SCENARIOS[name]
    monitor._clients["s3"] = stubs.FakeS3(objects)
    monitor._clients["ssm"] = stubs.FakeSSM(
        {WEBHOOK_PARAM: "https://hooks.slack.com/services/T000/B000/offline-fake"}
    )
    monitor._webhook_cache.clear()

    os.environ.update({
        "BUCKET": "cut-and-dry-test",
        "FEEDS": '[{"id":"test-dev","prefix":"test-dev/"}]',
        "STALE_DAYS": "3",
        "BUSINESS_DAYS_ONLY": "false",
        "SUFFIX": "",
        "WEBHOOK_PARAM": WEBHOOK_PARAM,
        "DISPLAY_TZ": "America/Los_Angeles",
        "DRY_RUN": "true",
        "MONITOR_ID": "s3-staleness-monitor",
    })

    posted = []
    real_post = monitor._post
    monitor._post = lambda text, cfg: posted.append(text)
    try:
        summary = monitor.lambda_handler({}, None)
    finally:
        monitor._post = real_post

    print(f"\n{'=' * 72}\n  {name}\n{'=' * 72}")
    print(f"  {description}\n")
    feed = summary["feeds"][0]
    elapsed = feed["elapsed_days"]
    print(f"  objects matched : {feed['objects']:,}")
    print(f"  newest file     : {feed['last_file_key'] or '(none)'}")
    print(f"  idle            : {'never' if elapsed is None else f'{elapsed:,.2f} days'}")
    print(f"  threshold       : {feed['threshold_days']:,} days")
    print(f"  status          : {feed['status']}")

    if posted:
        print("\n  --- would post to #slack-test " + "-" * 40)
        for line in posted[0].splitlines():
            print(f"  | {line}")
        print("  " + "-" * 70)
    else:
        print("\n  (silent - nothing posted)")

    if verbose:
        print("\n  summary:")
        print(json.dumps(summary, indent=2, default=str))

    expected_alert = name not in ("fresh", "just-under")
    ok = bool(posted) == expected_alert
    print(f"\n  expected {'an alert' if expected_alert else 'silence'}: "
          f"{'PASS' if ok else 'FAIL'}")
    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scenario", choices=sorted(SCENARIOS), help="run just one")
    ap.add_argument("--list", action="store_true", help="list scenarios and exit")
    ap.add_argument("-v", "--verbose", action="store_true", help="dump the full summary")
    ap.add_argument("--debug", action="store_true", help="show handler log output")
    args = ap.parse_args()

    if args.list:
        for n, (d, _) in sorted(SCENARIOS.items()):
            print(f"{n:<12} {d}")
        return 0

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.CRITICAL)

    names = [args.scenario] if args.scenario else list(SCENARIOS)
    results = {n: run(n, args.verbose) for n in names}

    print(f"\n{'=' * 72}")
    failed = [n for n, ok in results.items() if not ok]
    if failed:
        print(f"  {len(failed):,} of {len(results):,} scenarios FAILED: {', '.join(failed)}")
        return 1
    print(f"  all {len(results):,} scenarios behaved as expected")
    return 0


if __name__ == "__main__":
    sys.exit(main())
