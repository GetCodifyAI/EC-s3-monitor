#!/usr/bin/env python3
"""
Drive the non-prod monitor through a real STALE -> recovery cycle.

The vendor bucket lives in the prod account and is not readable from non-prod,
so non-prod watches a scratch bucket instead. S3 will not let you backdate
LastModified, so staleness is not simulated by ageing files - it is simulated by
an EMPTY prefix, which the monitor reports as "no files ever" and treats as
stale. Dropping one file clears it. That is the whole transition, with no
waiting for days to pass.

  python3 cfn/test_feed.py create    # make the scratch bucket (once)
  python3 cfn/test_feed.py status    # what the monitor will see right now
  python3 cfn/test_feed.py clear     # empty the prefix  -> next run alerts
  python3 cfn/test_feed.py drop      # add one file      -> next run recovers

Every command refuses to run outside the non-prod account, and refuses to touch
the vendor bucket under any circumstances. Dropping a file matching .*PO.*\\.csv
into the real prefix would be ingested by the live Snowpipe into PURCHASE_ORDERS.
"""
import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

HERE = Path(__file__).resolve().parent
ENV_FILE = HERE / "env" / "nonprod.env"

# Hard guards. Not configuration - do not move these into the env file.
FORBIDDEN_ACCOUNTS = {"057311931122"}          # prod
FORBIDDEN_BUCKETS = {"cut-dry-vendor-integration"}


def read_env(path):
    env = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.split("   #")[0].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        env[key.strip()] = value
    return env


def guard(env):
    """Refuse anything that could write into production."""
    bucket = env["MONITORED_BUCKET"]
    expected_account = env["ACCOUNT"]

    if bucket in FORBIDDEN_BUCKETS:
        sys.exit(f"REFUSING: {bucket} is the production vendor bucket.")
    if expected_account in FORBIDDEN_ACCOUNTS:
        sys.exit(f"REFUSING: env/nonprod.env points at production account {expected_account}.")

    ident = boto3.client("sts", region_name=env["REGION"]).get_caller_identity()
    if ident["Account"] in FORBIDDEN_ACCOUNTS:
        sys.exit(f"REFUSING: your credentials are for production ({ident['Account']}).")
    if ident["Account"] != expected_account:
        sys.exit(
            f"REFUSING: env/nonprod.env targets {expected_account}, "
            f"your credentials are for {ident['Account']} as {ident['Arn']}.\n"
            "Try: aws sso login --profile non-prod-sso"
        )
    print(f"    {ident['Arn']}")
    return ident


def feed(env):
    feeds = __import__("json").loads(env["FEEDS_JSON"])
    return env["MONITORED_BUCKET"], feeds[0]["prefix"]


def cmd_create(env, s3):
    bucket, _ = feed(env)
    region = env["REGION"]
    try:
        s3.create_bucket(
            Bucket=bucket,
            CreateBucketConfiguration={"LocationConstraint": region},
        )
        print(f"created s3://{bucket}")
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
            print(f"s3://{bucket} already exists")
        else:
            raise
    s3.put_public_access_block(
        Bucket=bucket,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True, "IgnorePublicAcls": True,
            "BlockPublicPolicy": True, "RestrictPublicBuckets": True,
        },
    )
    s3.put_bucket_tagging(
        Bucket=bucket,
        Tagging={"TagSet": [
            {"Key": "Project", "Value": "platform-monitors"},
            {"Key": "Monitor", "Value": "s3-feed-freshness"},
            {"Key": "Environment", "Value": "nonprod"},
            {"Key": "Owner", "Value": "data-eng"},
        ]},
    )
    print("    public access blocked, tagged Owner=data-eng")


def _objects(s3, bucket, prefix):
    out = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        out.extend(page.get("Contents", []))
    return out


def cmd_status(env, s3):
    bucket, prefix = feed(env)
    objs = _objects(s3, bucket, prefix)
    print(f"s3://{bucket}/{prefix}")
    if not objs:
        print("    empty  ->  monitor reports 'no files ever', feed is STALE")
        return
    newest = max(objs, key=lambda o: o["LastModified"])
    print(f"    {len(objs):,} object(s); newest {newest['Key'].rsplit('/', 1)[-1]}")
    print(f"    last modified {newest['LastModified'].astimezone().strftime('%m/%d/%y %H:%M %Z')}")
    if env.get("STALE_DAYS") == "1":
        print("    ->  at StaleDays=1 a file uploaded today still counts as one")
        print("        business day idle, so the feed reads STALE until tomorrow")
    else:
        print(f"    ->  feed is OK at StaleDays={env.get('STALE_DAYS', '3')}")


def cmd_clear(env, s3):
    bucket, prefix = feed(env)
    objs = _objects(s3, bucket, prefix)
    if not objs:
        print("prefix already empty")
        return
    s3.delete_objects(
        Bucket=bucket,
        Delete={"Objects": [{"Key": o["Key"]} for o in objs], "Quiet": True},
    )
    print(f"deleted {len(objs):,} object(s) from s3://{bucket}/{prefix}")
    print("    next run  ->  STALE alert in #slack-test")


HEADER = ("Short Name,Delivery Date,Distributor Product Code,Product Description,"
          "Pack & Size,Brand,UOM,PO Number,Qty Ordered,PO Net Weight,Fill Rate %")


def cmd_drop(env, s3):
    bucket, prefix = feed(env)
    now = datetime.now(timezone.utc)
    key = f"{prefix}test_PO_Data_{now.strftime('%Y%m%d')}_{now.strftime('%H%M%S')}.csv"
    rows = "\r\n".join(
        f"TESTCO,{now.strftime('%m/%d/%y')},SKU{i:05d},Synthetic test row {i},"
        f"6/1 GAL,TestBrand,CS,PO{now.strftime('%H%M%S')},{i * 2},{i * 11.5:.2f},100.0"
        for i in range(1, 26)
    )
    body = (HEADER + "\r\n" + rows + "\r\n").encode()
    s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType="text/csv")
    print(f"put s3://{bucket}/{key}  ({len(body):,} bytes, 25 rows)")
    print("    next run  ->  recovery message in #slack-test")


COMMANDS = {"create": cmd_create, "status": cmd_status, "clear": cmd_clear, "drop": cmd_drop}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=sorted(COMMANDS))
    args = ap.parse_args()

    env = read_env(ENV_FILE)
    print(f"==> env/nonprod.env -> account {env['ACCOUNT']}, {env['REGION']}")
    guard(env)
    COMMANDS[args.command](env, boto3.client("s3", region_name=env["REGION"]))


if __name__ == "__main__":
    main()
