import unittest
from unittest.mock import patch

from fastapi import HTTPException

from app import api, dispatcher
from app.events import Accepted, Quarantined, classify

DISRUPTION = {"id": "evt-1", "source": "tests", "booking_id": "booking-1", "flight_id": "SA101"}


class FakeDispatchStore:
    """In-memory stand-in with the same lease/owner semantics as PostgresStore."""

    def __init__(self):
        self.records = {"d1": {"dispatch_id": "d1", "app_id": "booking-worker", "method": "m",
                               "event_type": "com.example.booking.requested", "scheduled_at": None,
                               "payload": {"x": 1}, "attempts": 0, "status": "pending", "owner": None}}
        self.delays = []
        self.errors = []
        self.started = False

    def workflow_started(self, _record):
        return self.started

    def expire_lease(self, dispatch_id="d1"):
        self.records[dispatch_id]["status"] = "expired"

    def lease_dispatches(self, owner, limit, lease_seconds, dispatch_id=None):
        due = [r for r in self.records.values() if r["status"] in ("pending", "expired")
               and dispatch_id in (None, r["dispatch_id"])][:limit]
        for record in due:
            record.update(status="leased", attempts=record["attempts"] + 1, owner=owner)
        return [dict(r) for r in due]

    def _owned(self, dispatch_id, owner):
        record = self.records[dispatch_id]
        return record["status"] == "leased" and record["owner"] == owner

    def mark_scheduled(self, dispatch_id, owner):
        if not self._owned(dispatch_id, owner):
            return False
        self.records[dispatch_id]["scheduled_at"] = self.records[dispatch_id]["scheduled_at"] or "now"
        return True

    def complete_dispatch(self, dispatch_id, owner):
        if not self._owned(dispatch_id, owner):
            return False
        self.records[dispatch_id]["status"] = "dispatched"
        return True

    def release_dispatch(self, dispatch_id, owner, error, delay):
        if self._owned(dispatch_id, owner):
            self.records[dispatch_id]["status"] = "pending"
        self.errors.append(error)
        self.delays.append(delay)


def confirm(calls=None):
    def invoke(app_id, method, body):
        if calls is not None:
            calls.append(body)
        return {"scheduled": True}
    return invoke


class EventRegistryTests(unittest.TestCase):
    def test_flat_and_cloudevent_forms_share_workflow_id(self):
        flat = classify(DISRUPTION)
        wrapped = classify({"id": "evt-1", "source": "tests", "type": "com.example.flight.disrupted",
                            "data": {"booking_id": "booking-1", "flight_id": "SA101"}})
        self.assertIsInstance(flat, Accepted)
        self.assertEqual(flat.workflow_id, wrapped.workflow_id)
        self.assertEqual(flat.payload_hash, wrapped.payload_hash)

    def test_unknown_internal_invalid_and_extra_fields_are_quarantined(self):
        cases = [
            {**DISRUPTION, "type": "com.example.unknown"},
            {**DISRUPTION, "type": "com.example.booking.requested"},
            {"id": "e", "source": "s", "booking_id": "b"},
            {"id": "e", "source": "s", "data": {"booking_id": "b", "flight_id": "F", "extra": 1}},
            {"source": "s", "booking_id": "b", "flight_id": "F"},
            ["not", "an", "object"],
        ]
        for raw in cases:
            with self.subTest(raw=raw):
                self.assertIsInstance(classify(raw), Quarantined)

    def test_quarantine_id_is_stable_for_replays(self):
        self.assertEqual(classify({"id": "e"}).quarantine_id, classify({"id": "e"}).quarantine_id)

    def test_event_cannot_share_or_create_a_booking_by_idempotency_key(self):
        import hashlib
        key = "shared-key"
        booking_id = "booking-" + hashlib.sha256(key.encode()).hexdigest()[:40]
        event = classify({"id": key, "source": key, "booking_id": "b", "flight_id": "F"})
        self.assertNotEqual(event.workflow_id, booking_id)
        self.assertTrue(event.workflow_id.startswith("disruption-"))
        forged = classify({"id": key, "source": "s", "type": "com.example.booking.requested",
                           "data": {"workflow_id": booking_id, "booking_id": booking_id, "request": {}}})
        self.assertIsInstance(forged, Quarantined)


class DispatcherTests(unittest.TestCase):
    def test_worker_scheduling_is_idempotent_when_state_exists(self):
        import asyncio
        from types import SimpleNamespace
        from app import booking_worker
        scheduled = []
        client = SimpleNamespace(get_workflow_state=lambda *a, **k: object(),
                                 schedule_new_workflow=lambda *a, **k: scheduled.append(a))
        request = booking_worker.ScheduleRequest(workflow_id="w", booking_id="w", request={})
        with patch.object(booking_worker, "client", client):
            result = asyncio.run(booking_worker.schedule(request))
        self.assertEqual(scheduled, [])
        self.assertEqual(result["already_existed"], True)

    def test_disruption_worker_reports_existing_and_new_workflows(self):
        import asyncio
        from types import SimpleNamespace
        from app import disruption_worker
        scheduled, states = [], [None, object()]
        client = SimpleNamespace(get_workflow_state=lambda *a, **k: states[0],
                                 schedule_new_workflow=lambda *a, **k: scheduled.append(a))
        event = disruption_worker.DispatchEvent(workflow_id="w", booking_id="b", flight_id="SA101")
        with patch.object(disruption_worker, "client", client):
            created = asyncio.run(disruption_worker.schedule_disruption(event))
            states[0] = states[1]
            existing = asyncio.run(disruption_worker.schedule_disruption(event))
        self.assertEqual((created["already_existed"], existing["already_existed"]), (False, True))
        self.assertEqual(len(scheduled), 1)

    def test_crash_after_dapr_scheduling_before_marker_redelivers_safely(self):
        store = FakeDispatchStore()
        delivered = []

        def crash(*_args):
            raise RuntimeError("process died before the marker was written")

        real_mark = store.mark_scheduled
        store.mark_scheduled = crash
        invoke = confirm(delivered)
        # Dapr accepted the workflow, then the dispatcher died: no marker, record still leased.
        self.assertEqual(dispatcher.dispatch_due(store, invoke, owner="a"), 0)
        self.assertIsNone(store.records["d1"]["scheduled_at"])
        store.expire_lease()
        store.mark_scheduled = real_mark
        self.assertEqual(dispatcher.dispatch_due(store, invoke, owner="b"), 1)
        self.assertEqual(delivered, [{"x": 1}, {"x": 1}])  # same payload, same stable workflow ID
        self.assertEqual(store.records["d1"]["scheduled_at"], "now")

    def test_crash_after_marker_before_completion_does_not_reinvoke(self):
        store = FakeDispatchStore()
        calls = []
        store.complete_dispatch = lambda *_a: (_ for _ in ()).throw(RuntimeError("died"))
        with self.assertRaises(RuntimeError):
            dispatcher.dispatch_due(store, confirm(calls), owner="a")
        self.assertEqual(store.records["d1"]["scheduled_at"], "now")
        store.expire_lease()
        del store.complete_dispatch
        self.assertEqual(dispatcher.dispatch_due(store, confirm(calls), owner="b"), 1)
        self.assertEqual(len(calls), 1)

    def test_marker_prevents_rescheduling_after_dapr_history_purge(self):
        store = FakeDispatchStore()
        calls = []
        self.assertEqual(dispatcher.dispatch_due(store, confirm(calls)), 1)
        # Simulate a purged workflow and a replayed/reset lease: marker still blocks the worker call.
        store.records["d1"]["status"] = "expired"
        self.assertEqual(dispatcher.dispatch_due(store, confirm(calls)), 1)
        self.assertEqual(len(calls), 1)

    def test_replica_that_lost_its_lease_cannot_mark_or_complete(self):
        store = FakeDispatchStore()
        calls = []

        def slow_invoke(*args):
            calls.append(args[2])
            store.expire_lease()
            dispatcher.dispatch_due(store, confirm(calls), owner="b")  # replica b takes over
            return {"scheduled": True}

        self.assertEqual(dispatcher.dispatch_due(store, slow_invoke, owner="a"), 0)
        self.assertEqual(store.records["d1"]["status"], "dispatched")
        self.assertEqual(store.records["d1"]["owner"], "b")
        self.assertEqual(len(calls), 2)  # at-least-once; the worker treats the repeat as existing

    def test_unconfirmed_worker_response_is_retried(self):
        store = FakeDispatchStore()
        self.assertEqual(dispatcher.dispatch_due(store, lambda *a: {}), 0)
        self.assertIsNone(store.records["d1"]["scheduled_at"])
        self.assertEqual(store.records["d1"]["status"], "pending")

    def test_started_workflow_is_never_rescheduled_when_domain_rows_exist(self):
        store = FakeDispatchStore()
        store.started = True
        calls = []
        self.assertEqual(dispatcher.dispatch_due(store, confirm(calls)), 1)
        self.assertEqual(calls, [])
        self.assertEqual(store.records["d1"]["status"], "dispatched")

    def test_dispatch_failures_never_log_payload_contents(self):
        store = FakeDispatchStore()
        store.records["d1"]["payload"] = {"request": {"passenger_name": "Ada Lovelace"}}

        def invoke(*_args):
            raise HTTPException(503, "Ada Lovelace unavailable")

        with self.assertLogs("step4.dispatcher", level="WARNING") as logs:
            dispatcher.dispatch_due(store, invoke)
        self.assertNotIn("Ada", "".join(logs.output))
        self.assertNotIn("Ada", store.errors[0])

    def test_outage_is_retried_with_backoff_then_delivered(self):
        store = FakeDispatchStore()
        calls = []

        def invoke(app_id, method, body):
            calls.append(body)
            if len(calls) < 3:
                raise HTTPException(503, "down")
            return {"scheduled": True}

        self.assertEqual(dispatcher.dispatch_due(store, invoke), 0)
        self.assertEqual(dispatcher.dispatch_due(store, invoke), 0)
        self.assertEqual(dispatcher.dispatch_due(store, invoke), 1)
        self.assertEqual(store.delays, [1, 2])
        self.assertEqual(store.records["d1"]["status"], "dispatched")
        self.assertEqual(dispatcher.dispatch_due(store, invoke), 0)
        self.assertEqual(len(calls), 3)


class ApiAcceptanceTests(unittest.TestCase):
    def test_booking_accepted_even_when_dapr_is_down(self):
        booking = {"booking_id": "booking-1", "status": "scheduled"}
        request = api.BookingRequest(flight_id="SA101", seat="1A", passenger_name="Ada")
        with (
            patch.object(api.store, "create_booking", return_value=(booking, True)),
            patch.object(api.store, "lease_dispatches", return_value=[{
                "dispatch_id": "booking-x", "app_id": "booking-worker", "method": "m",
                "payload": {}, "attempts": 1, "event_type": "x", "scheduled_at": None}]),
            patch.object(api.store, "workflow_started", return_value=False),
            patch.object(api.store, "release_dispatch") as release,
            patch("app.api._invoke", side_effect=HTTPException(503, "down")),
        ):
            result = api.create_booking(request, idempotency_key="k")
        self.assertEqual(result["status"], "scheduled")
        release.assert_called_once()

    def test_changed_booking_payload_conflicts(self):
        request = api.BookingRequest(flight_id="SA101", seat="1A", passenger_name="Ada")
        with patch.object(api.store, "create_booking", side_effect=ValueError("different")):
            with self.assertRaises(HTTPException) as raised:
                api.create_booking(request, idempotency_key="k")
        self.assertEqual(raised.exception.status_code, 409)

    def test_invalid_disruption_is_quarantined_not_rejected(self):
        with patch.object(api.store, "quarantine_event") as quarantine:
            result = api.submit_disruption({"id": "e", "source": "s"})
        self.assertEqual(result["status"], "quarantined")
        quarantine.assert_called_once()

    def test_duplicate_disruption_skips_state_checks_and_dispatch(self):
        with (
            patch.object(api.store, "receipt", return_value={"workflow_id": "w"}),
            patch.object(api.store, "booking") as booking,
            patch.object(api.store, "accept_event", return_value=("w", False)),
            patch("app.api._try_dispatch") as dispatch,
        ):
            result = api.submit_disruption(dict(DISRUPTION))
        self.assertEqual(result["status"], "duplicate")
        booking.assert_not_called()
        dispatch.assert_not_called()

    def test_new_disruption_requires_confirmed_current_flight(self):
        stale = {"status": "confirmed", "ticket": {"flight_id": "SA102"}}
        with (
            patch.object(api.store, "receipt", return_value=None),
            patch.object(api.store, "booking", return_value=stale),
        ):
            with self.assertRaises(HTTPException) as raised:
                api.submit_disruption(dict(DISRUPTION))
        self.assertEqual(raised.exception.status_code, 409)

    def test_accepted_disruption_is_dispatched_and_changed_payload_conflicts(self):
        confirmed = {"status": "confirmed", "ticket": {"flight_id": "SA101"}}
        with (
            patch.object(api.store, "receipt", return_value=None),
            patch.object(api.store, "booking", return_value=confirmed),
            patch.object(api.store, "accept_event", return_value=("w", True)),
            patch("app.api._try_dispatch") as dispatch,
        ):
            self.assertEqual(api.submit_disruption(dict(DISRUPTION))["status"], "accepted")
        dispatch.assert_called_once_with("w")
        with (
            patch.object(api.store, "receipt", return_value={"workflow_id": "w"}),
            patch.object(api.store, "accept_event", side_effect=ValueError("different")),
        ):
            with self.assertRaises(HTTPException) as raised:
                api.submit_disruption(dict(DISRUPTION))
        self.assertEqual(raised.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main()
