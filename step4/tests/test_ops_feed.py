import logging
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from app import api, feed_poller
from app.events import Accepted, Quarantined, classify

STEP4 = Path(__file__).resolve().parents[1]


class ParseRowTests(unittest.TestCase):
    def parse(self, payload, threshold=60):
        return feed_poller.parse_row("feed-1", payload, threshold)

    def test_cancelled_and_long_delay_are_disruptive(self):
        self.assertTrue(self.parse({"flight_id": "SA101", "status": "cancelled"})[1])
        self.assertTrue(self.parse({"flight_id": "SA101", "status": "delayed", "delay_minutes": 60})[1])

    def test_minor_delay_and_on_time_are_not_disruptive(self):
        self.assertFalse(self.parse({"flight_id": "SA101", "status": "delayed", "delay_minutes": 59})[1])
        self.assertFalse(self.parse({"flight_id": "SA101", "status": "on_time"})[1])

    def test_threshold_is_configurable(self):
        payload = {"flight_id": "SA101", "status": "delayed", "delay_minutes": 30}
        self.assertTrue(self.parse(payload, threshold=30)[1])
        with patch.dict(os.environ, {"OPS_FEED_DELAY_THRESHOLD_MINUTES": "15"}):
            self.assertEqual(feed_poller.delay_threshold(), 15)

    def test_malformed_rows_are_quarantined_not_raised(self):
        for payload in (
            ["not", "an", "object"], {"flight_id": "SA101", "status": "exploded"},
            {"flight_id": "", "status": "cancelled"}, {"status": "cancelled"},
            {"flight_id": "SA101", "status": "cancelled", "extra": 1},
            {"flight_id": "SA101", "status": "delayed"},
            {"flight_id": "SA101", "status": "delayed", "delay_minutes": -5},
        ):
            with self.subTest(payload=payload):
                outcome = self.parse(payload)
                self.assertIsInstance(outcome, Quarantined)
                self.assertEqual(outcome.source, feed_poller.FEED_SOURCE)

    def test_overlong_feed_id_is_quarantined(self):
        outcome = feed_poller.parse_row("x" * 129, {"flight_id": "SA101", "status": "cancelled"}, 60)
        self.assertIsInstance(outcome, Quarantined)

    def test_quarantine_reason_does_not_echo_values(self):
        outcome = self.parse({"flight_id": "Jane Passenger", "status": "exploded"})
        self.assertNotIn("Jane", outcome.reason)

    def test_emitted_event_is_accepted_by_the_existing_registry(self):
        raw = {"id": "feed-1:booking-1", "source": feed_poller.FEED_SOURCE,
               "type": "com.example.flight.disrupted", "data": {"booking_id": "booking-1", "flight_id": "SA101"}}
        outcome = classify(raw)
        self.assertIsInstance(outcome, Accepted)
        self.assertTrue(outcome.workflow_id.startswith("disruption-"))


class SimulationEndpointTests(unittest.TestCase):
    def test_simulation_endpoint_is_404_unless_enabled(self):
        with patch.dict(os.environ, {}, clear=False), patch.object(api, "store") as store:
            os.environ.pop("OPS_FEED_SIMULATION", None)
            with self.assertRaises(HTTPException) as caught:
                api.seed_feed_event({"source_event_id": "a", "flight_id": "SA101", "status": "cancelled"})
            self.assertEqual(caught.exception.status_code, 404)
            store.insert_feed_event.assert_not_called()
        with patch.dict(os.environ, {"OPS_FEED_SIMULATION": "false"}):
            with self.assertRaises(HTTPException) as caught:
                api.seed_feed_event({"source_event_id": "a"})
            self.assertEqual(caught.exception.status_code, 404)

    def test_enabled_endpoint_seeds_and_reports_duplicates(self):
        with patch.dict(os.environ, {"OPS_FEED_SIMULATION": "true"}), patch.object(api, "store") as store:
            store.insert_feed_event.side_effect = [True, False]
            body = {"source_event_id": "a", "flight_id": "SA101", "status": "cancelled"}
            self.assertEqual(api.seed_feed_event(dict(body))["status"], "seeded")
            self.assertEqual(api.seed_feed_event(dict(body))["status"], "duplicate")
            store.insert_feed_event.assert_called_with("a", {"flight_id": "SA101", "status": "cancelled"})

    def test_enabled_endpoint_requires_a_source_event_id(self):
        with patch.dict(os.environ, {"OPS_FEED_SIMULATION": "true"}), patch.object(api, "store"):
            for body in ({}, {"source_event_id": ""}, {"source_event_id": 5}, {"source_event_id": "x" * 129}):
                with self.subTest(body=body), self.assertRaises(HTTPException) as caught:
                    api.seed_feed_event(body)
                self.assertEqual(caught.exception.status_code, 422)

    def test_polling_and_simulation_are_off_by_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPS_FEED_POLL_ENABLED", None)
            os.environ.pop("OPS_FEED_SIMULATION", None)
            self.assertFalse(api.feed_polling_enabled())
            self.assertFalse(api.feed_simulation_enabled())

    def test_checked_in_manifests_do_not_enable_polling_or_simulation(self):
        for manifest in (STEP4 / "k8s").glob("*.yaml"):
            self.assertNotIn("OPS_FEED", manifest.read_text(), manifest.name)
        self.assertNotIn("OPS_FEED", (STEP4 / "Dockerfile").read_text())

    def test_status_endpoint_exposes_only_cursor_and_counters(self):
        with patch.object(api, "store") as store:
            store.cursor.return_value = {"last_seq": 3, "partial_seq": None, "partial_booking_id": None}
            body = api.feed_status()
        self.assertEqual(set(body), {"enabled", "cursor", "counters"})


class PollLoggingTests(unittest.TestCase):
    def test_poll_logs_counts_only(self):
        stats = feed_poller.PollStats(polled=1, emitted=1)
        with patch.object(feed_poller, "run_once", return_value=stats), \
                self.assertLogs("step4.feed_poller", logging.INFO) as logs:
            feed_poller.poll_pass(object(), 20, 200)
        text = "\n".join(logs.output)
        self.assertIn("polled=1", text)
        self.assertNotIn("booking", text)

    def test_failure_is_counted_and_logged_by_type_only(self):
        before = feed_poller.counters.totals["failures"]
        with patch.object(feed_poller, "run_once", side_effect=RuntimeError("Jane Passenger")), \
                self.assertLogs("step4.feed_poller", logging.WARNING) as logs:
            with self.assertRaises(RuntimeError):
                feed_poller.poll_pass(object(), 20, 200)
        self.assertNotIn("Jane", "\n".join(logs.output))
        self.assertEqual(feed_poller.counters.totals["failures"], before + 1)


if __name__ == "__main__":
    unittest.main()
