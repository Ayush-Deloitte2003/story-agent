"""Bounded poller for the simulated operations feed.

The poller only creates disruption events. Each one goes through the same
registry, receipt and dispatch-record path as POST /disruptions, so workflows and
human approvals stay authoritative. One PostgreSQL transaction per pass writes
receipts, dispatch records, quarantines, flight status and the cursor together:
a crash before commit replays the pass, and a replay hits the event receipt key.
"""
import logging
import os
import uuid
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from app.events import FLIGHT_DISRUPTED, Accepted, Quarantined, canonical_hash, classify

CURSOR_NAME = "ops-feed"
FEED_SOURCE = "ops-feed-simulator"
FEED_EVENT_TYPE = "ops.flight.status"
MAX_SOURCE_EVENT_ID = 128
log = logging.getLogger("step4.feed_poller")


class FeedRow(BaseModel):
    model_config = {"extra": "forbid"}
    flight_id: str = Field(min_length=1, max_length=40)
    status: Literal["on_time", "delayed", "cancelled"]
    delay_minutes: int | None = Field(default=None, ge=0, le=100000)


@dataclass
class PollStats:
    polled: int = 0
    suppressed: int = 0
    emitted: int = 0
    duplicates: int = 0
    quarantined: int = 0
    skipped_lock: bool = False
    truncated: bool = False


@dataclass
class Counters:
    totals: dict = field(default_factory=lambda: {
        "passes": 0, "polled": 0, "suppressed": 0, "emitted": 0, "duplicates": 0, "quarantined": 0,
        "lock_skips": 0, "failures": 0})

    def add(self, stats: PollStats) -> None:
        self.totals["passes"] += 1
        self.totals["lock_skips"] += int(stats.skipped_lock)
        for key in ("polled", "suppressed", "emitted", "duplicates", "quarantined"):
            self.totals[key] += getattr(stats, key)


counters = Counters()


def delay_threshold() -> int:
    return int(os.getenv("OPS_FEED_DELAY_THRESHOLD_MINUTES", "60"))


def parse_row(source_event_id: str, payload, threshold: int) -> tuple[FeedRow, bool] | Quarantined:
    """Return (row, disruptive) or a Quarantined outcome; never raises for bad content."""
    def bad(reason: str) -> Quarantined:
        return Quarantined(reason, FEED_EVENT_TYPE, FEED_SOURCE, source_event_id[:256],
                           canonical_hash([reason, source_event_id, payload]))
    if len(source_event_id) > MAX_SOURCE_EVENT_ID:
        return bad("Feed event id is too long")
    if not isinstance(payload, dict):
        return bad("Feed row must be a JSON object")
    try:
        row = FeedRow.model_validate(payload)
    except ValidationError as exc:
        fields = sorted({".".join(str(p) for p in err["loc"]) for err in exc.errors()})
        return bad(f"Invalid feed row: {', '.join(fields)}")
    if row.status == "delayed" and row.delay_minutes is None:
        return bad("Invalid feed row: delay_minutes")
    disruptive = row.status == "cancelled" or (row.status == "delayed" and row.delay_minutes >= threshold)
    return row, disruptive


def run_once(store, batch_size: int = 20, max_events: int = 200, threshold: int | None = None,
             cursor_name: str = CURSOR_NAME) -> PollStats:
    """One bounded pass. At most max_events disruption events are created per pass.

    A feed row whose fan-out would exceed the cap is resumed on the next pass via
    the partial cursor (partial_seq, partial_booking_id); its flight status is only
    recorded once the whole row has been emitted.
    """
    threshold = delay_threshold() if threshold is None else threshold
    max_events = max(1, max_events)
    stats = PollStats()
    with store._connect() as conn:
        cursor = conn.execute(
            """SELECT last_seq, partial_seq, partial_booking_id FROM poller_cursors
               WHERE name = %s FOR UPDATE SKIP LOCKED""", (cursor_name,)).fetchone()
        if cursor is None:
            stats.skipped_lock = True
            return stats
        rows = conn.execute(
            """SELECT seq, source_event_id, payload FROM ops_feed_events
               WHERE seq > %s AND txid < pg_snapshot_xmin(pg_current_snapshot())
               ORDER BY seq LIMIT %s""", (cursor["last_seq"], max(1, batch_size))).fetchall()
        budget = max_events
        last_seq, partial = cursor["last_seq"], (cursor["partial_seq"], cursor["partial_booking_id"])
        for item in rows:
            seq, source_event_id, payload = item["seq"], item["source_event_id"], item["payload"]
            stats.polled += 1
            parsed = parse_row(source_event_id, payload, threshold)
            known = isinstance(parsed, tuple) and conn.execute(
                "SELECT 1 FROM flight_inventory WHERE flight_id = %s", (parsed[0].flight_id,)).fetchone()
            if isinstance(parsed, tuple) and not known:
                parsed = Quarantined("Unknown flight", FEED_EVENT_TYPE, FEED_SOURCE, source_event_id[:256],
                                     canonical_hash(["Unknown flight", source_event_id, payload]))
            if isinstance(parsed, Quarantined):
                store._quarantine_event(conn, parsed, payload)
                stats.quarantined += 1
            else:
                feed_row, disruptive = parsed
                prior = conn.execute("SELECT status, disruptive FROM ops_flight_status WHERE flight_id = %s",
                                     (feed_row.flight_id,)).fetchone()
                changed = prior is None or (prior["status"], prior["disruptive"]) != (feed_row.status, disruptive)
                if not (changed and disruptive):
                    stats.suppressed += 1
                else:
                    after = partial[1] if partial[0] == seq else ""
                    booking_ids = [r["booking_id"] for r in conn.execute(
                        """SELECT t.booking_id FROM tickets t JOIN bookings b USING (booking_id)
                           WHERE t.flight_id = %s AND b.status = 'confirmed' AND t.booking_id > %s
                           ORDER BY t.booking_id LIMIT %s""",
                        (feed_row.flight_id, after or "", budget + 1)).fetchall()]
                    take, truncated = booking_ids[:budget], len(booking_ids) > budget
                    for booking_id in take:
                        _emit(store, conn, stats, source_event_id, booking_id, feed_row.flight_id)
                    budget -= len(take)
                    if truncated:
                        conn.execute(
                            """UPDATE poller_cursors SET partial_seq = %s, partial_booking_id = %s,
                               updated_at = now() WHERE name = %s""", (seq, take[-1], cursor_name))
                        stats.truncated = True
                        break
                conn.execute(
                    """INSERT INTO ops_flight_status(flight_id, status, disruptive, last_seq)
                       VALUES (%s, %s, %s, %s) ON CONFLICT (flight_id) DO UPDATE
                       SET status = EXCLUDED.status, disruptive = EXCLUDED.disruptive,
                           last_seq = EXCLUDED.last_seq""",
                    (feed_row.flight_id, feed_row.status, disruptive, seq))
            last_seq, partial = seq, (None, None)
            conn.execute(
                """UPDATE poller_cursors SET last_seq = %s, partial_seq = NULL, partial_booking_id = NULL,
                   updated_at = now() WHERE name = %s""", (seq, cursor_name))
            if budget <= 0:
                stats.truncated = stats.truncated or len(rows) > stats.polled
                break
    return stats


def _emit(store, conn, stats: PollStats, source_event_id: str, booking_id: str, flight_id: str) -> None:
    raw = {"id": f"{source_event_id}:{booking_id}", "source": FEED_SOURCE, "type": FLIGHT_DISRUPTED,
           "data": {"booking_id": booking_id, "flight_id": flight_id}}
    outcome = classify(raw)
    if not isinstance(outcome, Accepted):
        store._quarantine_event(conn, outcome, raw)
        stats.quarantined += 1
        return
    _, created = store._accept_event(conn, outcome)
    if created:
        stats.emitted += 1
    else:
        stats.duplicates += 1


def poll_pass(store, batch_size: int, max_events: int) -> PollStats:
    poll_id = uuid.uuid4().hex[:12]
    try:
        stats = run_once(store, batch_size, max_events)
    except Exception as exc:
        counters.totals["failures"] += 1
        log.warning("poll %s failed: %s", poll_id, type(exc).__name__)
        raise
    counters.add(stats)
    if stats.polled or stats.skipped_lock:
        log.info("poll %s polled=%d suppressed=%d emitted=%d duplicates=%d quarantined=%d "
                 "truncated=%s lock_skipped=%s", poll_id, stats.polled, stats.suppressed,
                 stats.emitted, stats.duplicates, stats.quarantined, stats.truncated, stats.skipped_lock)
    return stats
