#!/usr/bin/env python3
"""
Unit tests for the staleness monitor.

    python3 -m unittest test_monitor -v
    python3 test_monitor.py

No AWS, no network, no third-party packages. Concentrated on the parts that can
be silently wrong for a long time: the threshold boundary, what counts as a
file, and the fact that a healthy run must post nothing at all.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

import stubs

stubs.install()

import monitor

WEBHOOK = "/platform-monitors/s3-staleness/slack-test-webhook"
GOOD_URL = "https://hooks.slack.com/services/T000/B000/xxxx"


def base_env(**over):
    env = {
        "BUCKET": "cut-and-dry-test",
        "FEEDS": '[{"id":"test-dev","prefix":"test-dev/"}]',
        "STALE_DAYS": "3",
        "BUSINESS_DAYS_ONLY": "false",
        "SUFFIX": "",
        "WEBHOOK_PARAM": WEBHOOK,
        "DISPLAY_TZ": "America/Los_Angeles",
        "DRY_RUN": "true",
        "MONITOR_ID": "s3-staleness-monitor",
    }
    env.update(over)
    return env


class HandlerCase(unittest.TestCase):
    """Drives the real lambda_handler against a fake S3, capturing Slack posts."""

    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.posted = []
        self._real_post = monitor._post
        monitor._post = lambda text, cfg: self.posted.append(text)
        monitor._webhook_cache.clear()
        monitor._clients["ssm"] = stubs.FakeSSM({WEBHOOK: GOOD_URL})
        self._saved_env = dict(os.environ)

    def tearDown(self):
        monitor._post = self._real_post
        os.environ.clear()
        os.environ.update(self._saved_env)

    def run_with(self, objects, **env):
        monitor._clients["s3"] = stubs.FakeS3(objects)
        os.environ.update(base_env(**env))
        return monitor.lambda_handler({}, None)

    def obj(self, key, days_ago, size=1024):
        return stubs.obj(key, days_ago, size, now=self.now)


class TestThreshold(HandlerCase):
    def test_fresh_feed_is_silent(self):
        """The healthy path posts nothing. If this ever fails, the channel gets spammed."""
        s = self.run_with([self.obj("test-dev/a.csv", 0.5)])
        self.assertEqual(s["prefixes_stale"], 0)
        self.assertEqual(s["feeds"][0]["status"], "OK")
        self.assertEqual(self.posted, [])

    def test_exactly_at_threshold_alerts(self):
        """3.0 days with a 3-day threshold is stale: the comparison is >=, not >."""
        s = self.run_with([self.obj("test-dev/a.csv", 3.0)])
        self.assertEqual(s["feeds"][0]["status"], "STALE")
        self.assertEqual(len(self.posted), 1)

    def test_just_under_threshold_is_silent(self):
        s = self.run_with([self.obj("test-dev/a.csv", 2.99)])
        self.assertEqual(s["feeds"][0]["status"], "OK")
        self.assertEqual(self.posted, [])

    def test_well_past_threshold_alerts(self):
        s = self.run_with([self.obj("test-dev/a.csv", 11.5)])
        self.assertEqual(s["feeds"][0]["status"], "STALE")
        self.assertIn("idle 11.5 days", self.posted[0])

    def test_empty_prefix_alerts_as_never(self):
        """No objects at all must be stale, not an unnoticed zero."""
        s = self.run_with([])
        self.assertEqual(s["feeds"][0]["status"], "STALE")
        self.assertIsNone(s["feeds"][0]["last_file_at"])
        self.assertIn("no file has ever landed here", self.posted[0])

    def test_per_feed_threshold_overrides_default(self):
        s = self.run_with(
            [self.obj("test-dev/a.csv", 5.0)],
            FEEDS='[{"id":"test-dev","prefix":"test-dev/","stale_days":10}]',
        )
        self.assertEqual(s["feeds"][0]["status"], "OK")
        self.assertEqual(self.posted, [])


class TestWhatCountsAsAFile(HandlerCase):
    def test_zero_byte_marker_does_not_reset_the_clock(self):
        """A fresh _SUCCESS marker over a stale real file must still alert."""
        s = self.run_with([
            self.obj("test-dev/_SUCCESS", 0.01, size=0),
            self.obj("test-dev/a.csv", 8.0),
        ])
        self.assertEqual(s["feeds"][0]["status"], "STALE")
        self.assertEqual(s["feeds"][0]["objects"], 1)

    def test_directory_placeholder_is_ignored(self):
        s = self.run_with([
            self.obj("test-dev/", 0.01, size=0),
            self.obj("test-dev/a.csv", 8.0),
        ])
        self.assertEqual(s["feeds"][0]["objects"], 1)
        self.assertEqual(s["feeds"][0]["status"], "STALE")

    def test_suffix_filter_excludes_other_types(self):
        s = self.run_with(
            [self.obj("test-dev/a.tmp", 0.1), self.obj("test-dev/b.csv", 9.0)],
            SUFFIX=".csv",
        )
        self.assertEqual(s["feeds"][0]["objects"], 1)
        self.assertEqual(s["feeds"][0]["status"], "STALE")

    def test_suffix_match_is_case_insensitive(self):
        s = self.run_with([self.obj("test-dev/A.CSV", 0.1)], SUFFIX=".csv")
        self.assertEqual(s["feeds"][0]["objects"], 1)
        self.assertEqual(s["feeds"][0]["status"], "OK")

    def test_objects_outside_the_prefix_are_not_counted(self):
        s = self.run_with([
            self.obj("other-dev/fresh.csv", 0.1),
            self.obj("test-dev/old.csv", 7.0),
        ])
        self.assertEqual(s["feeds"][0]["objects"], 1)
        self.assertEqual(s["feeds"][0]["status"], "STALE")

    def test_newest_wins_regardless_of_listing_order(self):
        s = self.run_with([
            self.obj("test-dev/old.csv", 9.0),
            self.obj("test-dev/new.csv", 0.2),
            self.obj("test-dev/mid.csv", 4.0),
        ])
        self.assertEqual(s["feeds"][0]["status"], "OK")
        self.assertTrue(s["feeds"][0]["last_file_key"].endswith("new.csv"))
        self.assertEqual(s["feeds"][0]["objects"], 3)


class TestBusinessDays(unittest.TestCase):
    """_elapsed_days in isolation - the calendar maths is easiest to get wrong."""

    TZ = "America/Los_Angeles"

    def elapsed(self, last, now, business):
        return monitor._elapsed_days(last, now, business, self.TZ)

    def test_never_is_infinite(self):
        now = datetime(2026, 9, 4, 12, tzinfo=timezone.utc)
        self.assertEqual(self.elapsed(None, now, False), float("inf"))
        self.assertEqual(self.elapsed(None, now, True), float("inf"))

    def test_calendar_days_are_continuous(self):
        now = datetime(2026, 9, 4, 12, tzinfo=timezone.utc)
        last = now - timedelta(days=2, hours=12)
        self.assertAlmostEqual(self.elapsed(last, now, False), 2.5, places=2)

    def test_friday_to_monday_is_one_business_day(self):
        """
        The reason the flag exists. Fri 09/04/26 -> Mon 09/07/26 is three
        calendar days but one business day, so a Friday drop does not read as
        stale on Monday morning.
        """
        friday = datetime(2026, 9, 4, 17, tzinfo=timezone.utc)
        monday = datetime(2026, 9, 7, 17, tzinfo=timezone.utc)
        self.assertEqual(self.elapsed(friday, monday, True), 1.0)
        self.assertAlmostEqual(self.elapsed(friday, monday, False), 3.0, places=2)

    def test_same_day_is_zero_business_days(self):
        morning = datetime(2026, 9, 2, 15, tzinfo=timezone.utc)
        evening = datetime(2026, 9, 2, 23, tzinfo=timezone.utc)
        self.assertEqual(self.elapsed(morning, evening, True), 0.0)

    def test_a_full_week_is_five_business_days(self):
        a = datetime(2026, 9, 2, 17, tzinfo=timezone.utc)
        b = a + timedelta(days=7)
        self.assertEqual(self.elapsed(a, b, True), 5.0)


class TestWebhookResolution(unittest.TestCase):
    def setUp(self):
        monitor._webhook_cache.clear()

    def test_bare_url_is_accepted(self):
        monitor._clients["ssm"] = stubs.FakeSSM({WEBHOOK: GOOD_URL})
        self.assertEqual(monitor._webhook(WEBHOOK), GOOD_URL)

    def test_json_wrapped_url_is_accepted(self):
        monitor._clients["ssm"] = stubs.FakeSSM(
            {WEBHOOK: '{"webhook_url": "%s"}' % GOOD_URL}
        )
        self.assertEqual(monitor._webhook(WEBHOOK), GOOD_URL)

    def test_non_slack_url_is_rejected(self):
        """Never POST a payload at whatever happens to be in the parameter."""
        monitor._clients["ssm"] = stubs.FakeSSM({WEBHOOK: "https://evil.example/x"})
        with self.assertRaises(RuntimeError):
            monitor._webhook(WEBHOOK)

    def test_missing_parameter_raises(self):
        monitor._clients["ssm"] = stubs.FakeSSM({})
        with self.assertRaises(stubs.ParameterNotFound):
            monitor._webhook(WEBHOOK)


class TestDryRunAndMessage(HandlerCase):
    def test_dry_run_checks_the_webhook(self):
        """A clean dry run must still prove the webhook resolves."""
        s = self.run_with([self.obj("test-dev/a.csv", 0.1)])
        self.assertTrue(s["dry_run"])
        self.assertTrue(s["webhook_check"]["ok"])
        self.assertEqual(s["webhook_check"]["detail"], "hooks.slack.com")

    def test_webhook_check_reports_failure_without_raising(self):
        monitor._clients["ssm"] = stubs.FakeSSM({WEBHOOK: "not-a-url"})
        s = self.run_with([self.obj("test-dev/a.csv", 0.1)])
        self.assertFalse(s["webhook_check"]["ok"])

    def test_webhook_url_never_appears_in_the_summary(self):
        s = self.run_with([self.obj("test-dev/a.csv", 0.1)])
        self.assertNotIn(GOOD_URL, str(s))

    def test_message_uses_mm_dd_yy_and_thousands_separators(self):
        objects = [self.obj(f"test-dev/f{i}.csv", 6.0 + i / 100) for i in range(1200)]
        self.run_with(objects)
        text = self.posted[0]
        self.assertIn("1,200 files in prefix", text)
        self.assertRegex(text, r"last file \d{2}/\d{2}/\d{2} \d{2}:\d{2}")

    def test_message_names_bucket_prefix_and_threshold(self):
        self.run_with([self.obj("test-dev/a.csv", 7.0)])
        text = self.posted[0]
        self.assertIn("s3://cut-and-dry-test/test-dev/", text)
        self.assertIn("threshold 3 days", text)
        self.assertIn("s3-staleness-monitor", text)

    def test_singular_day_is_not_pluralised(self):
        self.run_with([self.obj("test-dev/a.csv", 2.0)], STALE_DAYS="1")
        self.assertIn("threshold 1 day)", self.posted[0])


class TestMultiplePrefixes(HandlerCase):
    FEEDS = ('[{"id":"test-dev","prefix":"test-dev/"},'
             '{"id":"test-uat","prefix":"test-uat/"}]')

    def test_only_the_stale_prefix_is_reported(self):
        s = self.run_with(
            [self.obj("test-dev/a.csv", 0.2), self.obj("test-uat/b.csv", 9.0)],
            FEEDS=self.FEEDS,
        )
        self.assertEqual(s["prefixes_checked"], 2)
        self.assertEqual(s["prefixes_stale"], 1)
        text = self.posted[0]
        self.assertIn("1 of 2 prefixes stale", text)
        self.assertIn("test-uat", text)

    def test_all_healthy_is_silent(self):
        self.run_with(
            [self.obj("test-dev/a.csv", 0.2), self.obj("test-uat/b.csv", 0.3)],
            FEEDS=self.FEEDS,
        )
        self.assertEqual(self.posted, [])

    def test_one_slack_post_covers_every_stale_prefix(self):
        """Aggregated, not one message per prefix."""
        self.run_with(
            [self.obj("test-dev/a.csv", 8.0), self.obj("test-uat/b.csv", 9.0)],
            FEEDS=self.FEEDS,
        )
        self.assertEqual(len(self.posted), 1)
        self.assertIn("2 of 2 prefixes stale", self.posted[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
