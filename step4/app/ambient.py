"""Ambient intake for customer-channel events (simulated).

Booking and cancellation requests can arrive as events instead of REST calls. The poller turns each
event into the same durable booking or cancellation record, with the same dispatch intent, that
POST /bookings and POST /cancellations create, so the workflows, advisors and human approvals are
unchanged and no second pipeline exists. Delivery is at-least-once; the idempotency key derived from
the source event id makes the effect idempotent. Notify and question items go to attention_items and
never carry event payloads.
"""
import hashlib
import logging
import os
from dataclasses import dataclass, field

from pydantic import ValidationError

from app.contracts import BookingRequest, CancellationRequest, booking_payload, cancellation_payload
from app.events import Quarantined, canonical_hash

SOURCE = "customer-channel-simulator"
KINDS = ("booking", "cancellation")
MAX_SOURCE_EVENT_ID = 128
log = logging.getLogger("step4.ambient")


@dataclass(frozen=True)
class Question:
    reason: str


@dataclass(frozen=True)
class Plan:
    kind: str
    workflow_id: str
    idempotency_key: str
    payload: dict


def cursor_name(kind: str) -> str:
    return f"ambient-{kind}"


def enabled_kinds(value: str | None = None) -> list[str]:
    raw = os.getenv("AMBIENT_FEEDS", "") if value is None else value
    return [k for k in KINDS if k in {part.strip() for part in raw.split(",")}]


def _derive(kind: str, source_event_id: str) -> tuple[str, str]:
    key = f"ambient:{kind}:{source_event_id}"
    return key, f"{kind}-" + hashlib.sha256(key.encode()).hexdigest()[:40]


def parse(kind: str, source_event_id: str, payload, failure_injection: bool) -> Plan | Quarantined | Question:
    """Validate one event; never raises for bad content."""
    def bad(reason: str) -> Quarantined:
        return Quarantined(reason, f"ambient.{kind}", SOURCE, source_event_id[:256],
                           canonical_hash([reason, kind, source_event_id, payload]))
    if len(source_event_id) > MAX_SOURCE_EVENT_ID:
        return bad("Ambient event id is too long")
    if not isinstance(payload, dict):
        return bad("Ambient event must be a JSON object")
    try:
        if kind == "booking":
            if not payload.get("seat"):
                return Question("A seat is required before this booking request can proceed")
            body = booking_payload(BookingRequest.model_validate(payload), failure_injection)
        else:
            body = cancellation_payload(CancellationRequest.model_validate(payload), failure_injection)
    except ValidationError as exc:
        fields = sorted({".".join(str(p) for p in err["loc"]) for err in exc.errors()})
        return bad(f"Invalid {kind} event: {', '.join(fields)}")
    except ValueError as exc:
        return bad(str(exc))
    key, workflow_id = _derive(kind, source_event_id)
    return Plan(kind, workflow_id, key, body)


@dataclass
class Stats:
    polled: int = 0
    created: int = 0
    duplicates: int = 0
    quarantined: int = 0
    questions: int = 0
    skipped_lock: bool = False


@dataclass
class Counters:
    by_kind: dict = field(default_factory=lambda: {
        kind: {"passes": 0, "polled": 0, "created": 0, "duplicates": 0, "quarantined": 0,
               "questions": 0, "lock_skips": 0, "failures": 0} for kind in KINDS})

    def add(self, kind: str, stats: Stats) -> None:
        totals = self.by_kind[kind]
        totals["passes"] += 1
        totals["lock_skips"] += int(stats.skipped_lock)
        for key in ("polled", "created", "duplicates", "quarantined", "questions"):
            totals[key] += getattr(stats, key)


counters = Counters()


def _attention_id(kind: str, source_event_id: str, label: str) -> str:
    return hashlib.sha256(f"{label}:{kind}:{source_event_id}".encode()).hexdigest()[:40]


def run_once(store, kind: str, batch_size: int = 20, failure_injection: bool = False) -> Stats:
    """One bounded pass over one feed kind: one PostgreSQL transaction covers every effect and the cursor."""
    stats = Stats()
    name = cursor_name(kind)
    with store._connect() as conn:
        cursor = conn.execute("SELECT last_seq FROM poller_cursors WHERE name = %s FOR UPDATE SKIP LOCKED",
                              (name,)).fetchone()
        if cursor is None:
            stats.skipped_lock = True
            return stats
        rows = conn.execute(
            """SELECT seq, source_event_id, payload FROM ambient_events
               WHERE kind = %s AND seq > %s AND txid < pg_snapshot_xmin(pg_current_snapshot())
               ORDER BY seq LIMIT %s""", (kind, cursor["last_seq"], max(1, batch_size))).fetchall()
        for item in rows:
            stats.polled += 1
            event_id = item["source_event_id"]
            parsed = parse(kind, event_id, item["payload"], failure_injection)
            if isinstance(parsed, Plan):
                try:
                    with conn.transaction():
                        create = store.create_booking if kind == "booking" else store.create_cancellation
                        _, created = create(parsed.workflow_id, parsed.idempotency_key, parsed.payload, conn=conn)
                    stats.created += int(created)
                    stats.duplicates += int(not created)
                except (ValueError, LookupError) as exc:
                    parsed = Quarantined(str(exc), f"ambient.{kind}", SOURCE, event_id[:256],
                                         canonical_hash([str(exc), kind, event_id, item["payload"]]))
            if isinstance(parsed, Quarantined):
                store._quarantine_event(conn, parsed, item["payload"])
                store._add_attention(conn, _attention_id(kind, event_id, "quarantine"), "notify",
                                     f"{kind} event {event_id[:64]}",
                                     f"Event was quarantined and not processed: {parsed.reason[:200]}")
                stats.quarantined += 1
            elif isinstance(parsed, Question):
                store._add_attention(conn, _attention_id(kind, event_id, "question"), "question",
                                     f"{kind} event {event_id[:64]}", parsed.reason)
                stats.questions += 1
            conn.execute("UPDATE poller_cursors SET last_seq = %s, updated_at = now() WHERE name = %s",
                         (item["seq"], name))
    return stats


def poll_pass(store, kind: str, batch_size: int, failure_injection: bool) -> Stats:
    try:
        stats = run_once(store, kind, batch_size, failure_injection)
    except Exception as exc:
        counters.by_kind[kind]["failures"] += 1
        log.warning("ambient %s poll failed: %s", kind, type(exc).__name__)
        raise
    counters.add(kind, stats)
    if stats.polled:
        log.info("ambient %s polled=%d created=%d duplicates=%d quarantined=%d questions=%d",
                 kind, stats.polled, stats.created, stats.duplicates, stats.quarantined, stats.questions)
    return stats
