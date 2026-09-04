"""
Offline stubs for boto3, shared by run_local.py and test_monitor.py.

Installs fake boto3 / botocore modules into sys.modules so src/monitor.py can be
imported and executed on a laptop with no AWS credentials, no network and no
boto3 installed. The handler under test is the real one - only the two AWS
clients it reaches for are replaced.
"""

import sys
import types
from datetime import datetime, timedelta, timezone


class FakePaginator:
    def __init__(self, objects):
        self._objects = objects

    def paginate(self, Bucket=None, Prefix=""):  # noqa: N803 - boto3's casing
        matching = [o for o in self._objects if o["Key"].startswith(Prefix)]
        # Two pages, so pagination handling is actually exercised rather than
        # assumed. A single-page fake would hide a missing paginator.
        mid = (len(matching) + 1) // 2
        for chunk in (matching[:mid], matching[mid:]):
            yield {"Contents": chunk} if chunk else {}


class FakeS3:
    def __init__(self, objects):
        self.objects = objects

    def get_paginator(self, name):
        assert name == "list_objects_v2", f"unexpected paginator: {name}"
        return FakePaginator(self.objects)


class ParameterNotFound(Exception):
    pass


class FakeSSMExceptions:
    ParameterNotFound = ParameterNotFound


class FakeSSM:
    exceptions = FakeSSMExceptions()

    def __init__(self, params=None):
        self.params = params or {}

    def get_parameter(self, Name=None, WithDecryption=False):  # noqa: N803
        if Name not in self.params:
            raise ParameterNotFound(f"{Name} not found")
        return {"Parameter": {"Name": Name, "Value": self.params[Name]}}


def install():
    """Put fake boto3 / botocore into sys.modules. Safe to call more than once."""
    if "boto3" in sys.modules and getattr(sys.modules["boto3"], "_is_stub", False):
        return

    boto3 = types.ModuleType("boto3")
    boto3._is_stub = True
    boto3.client = lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("stub boto3: inject clients via monitor._clients instead")
    )
    sys.modules["boto3"] = boto3

    botocore = types.ModuleType("botocore")
    exceptions = types.ModuleType("botocore.exceptions")

    class ClientError(Exception):
        pass

    exceptions.ClientError = ClientError
    botocore.exceptions = exceptions
    sys.modules["botocore"] = botocore
    sys.modules["botocore.exceptions"] = exceptions


def obj(key, days_ago, size=1024, now=None):
    """One S3 listing entry, aged relative to now."""
    now = now or datetime.now(timezone.utc)
    return {
        "Key": key,
        "Size": size,
        "LastModified": now - timedelta(days=days_ago),
    }
