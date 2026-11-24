"""Ambient intake for customer-channel events (simulated).

Booking and cancellation requests can arrive as events instead of REST calls. The poller turns each
event into the same durable booking or cancellation record, with the same dispatch intent, that
POST /bookings and POST /cancellations create, so the workflows, advisors and human approvals are
unchanged and no second pipeline exists. Delivery is at-least-once; the idempotency key derived from
the source event id makes the effect idempotent. Notify and question items go to attention_items and
never carry event payloads.
"""
import hashlib
import json
import logging
import os
import urllib.request
from dataclasses import dataclass, field

from pydantic import BaseModel, Field, ValidationError

from app.contracts import BookingRequest, CancellationRequest, booking_payload, cancellation_payload
from app.events import Quarantined, canonical_hash

SOURCE = "customer-channel-simulator"
KINDS = ("booking", "cancellation", "support")
MAX_SOURCE_EVENT_ID = 128
log = logging.getLogger("step4.ambient")


class SupportEvent(BaseModel):
    model_config = {"extra": "forbid"}
    booking_id: str = Field(min_length=1, max_length=100)
    question: str = Field(min_length=1, max_length=500)


def ask_support_service(body: dict) -> dict:
    """Read-only call to the separate support service; its answer is only ever a draft for review."""
    url = os.getenv("SUPPORT_SERVICE_URL", "http://support-service:8000") + "/support/ask"
    request = urllib.request.Request(url, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=float(os.getenv("SUPPORT_ASK_TIMEOUT_SECONDS", "10"))) as r:
        return json.loads(r.read())


@dataclass(frozen=True)
class Question:
    reason: str


@dataclass(frozen=True)
class Plan:
    kind: str
    workflow_id: str
    idempotency_key: str
    payload: dict


@dataclass(frozen=True)
class SupportPlan:
    booking_id: str
    question: str


def cursor_name(kind: str) -> str:
    return f"ambient-{kind}"


def enabled_kinds(value: str | None = None) -> list[str]:
    raw = os.getenv("AMBIENT_FEEDS", "") if value is None else value
    return [k for k in KINDS if k in {part.strip() for part in raw.split(",")}]


def _derive(kind: str, source_event_id: str) -> tuple[str, str]:
    key = f"ambient:{kind}:{source_event_id}"
    return key, f"{kind}-" + hashlib.sha256(key.encode()).hexdigest()[:40]


def parse(kind: str, source_event_id: str, payload,
          failure_injection: bool) -> Plan | SupportPlan | Quarantined | Question:
    """Validate one event; never raises for bad content."""
    def bad(reason: str) -> Quarantined:
        return Quarantined(reason, f"ambient.{kind}", SOURCE, source_event_id[:256],
                           canonical_hash([reason, kind, source_event_id, payload]))
    if len(source_event_id) > MAX_SOURCE_EVENT_ID:
        return bad("Ambient event id is too long")
    if not isinstance(payload, dict):
        return bad("Ambient event must be a JSON object")
    try:
        if kind == "support":
            event = SupportEvent.model_validate(payload)
            return SupportPlan(event.booking_id, event.question)
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
    reviews: int = 0
    answer_failures: int = 0
    skipped_lock: bool = False


@dataclass
class Counters:
    by_kind: dict = field(default_factory=lambda: {
        kind: {"passes": 0, "polled": 0, "created": 0, "duplicates": 0, "quarantined": 0,
               "questions": 0, "reviews": 0, "answer_failures": 0, "lock_skips": 0,
               "failures": 0} for kind in KINDS})

    def add(self, kind: str, stats: Stats) -> None:
        totals = self.by_kind[kind]
        totals["passes"] += 1
        totals["lock_skips"] += int(stats.skipped_lock)
        for key in ("polled", "created", "duplicates", "quarantined", "questions", "reviews",
                    "answer_failures"):
            totals[key] += getattr(stats, key)


counters = Counters()


def _attention_id(kind: str, source_event_id: str, label: str) -> str:
    return hashlib.sha256(f"{label}:{kind}:{source_event_id}".encode()).hexdigest()[:40]


def review_retention_minutes() -> int:
    return int(os.getenv("SUPPORT_REVIEW_RETENTION_MINUTES", "60"))


def _review_support(store, conn, stats: Stats, event_id: str, plan: SupportPlan, answer) -> None:
    """Ask the read-only support service for a draft and queue it for human review; nothing is sent."""
    draft = None
    for _ in range(2):
        try:
            draft = answer({"booking_id": plan.booking_id, "question": plan.question})
            break
        except Exception:  # unknown booking, timeout or outage: no detail is logged or stored
            continue
    subject = f"support {plan.booking_id[:80]} ({event_id[:40]})"
    if not isinstance(draft, dict) or not isinstance(draft.get("answer_draft"), str):
        store._add_attention(conn, _attention_id("support", event_id, "unanswered"), "notify", subject,
                             "No draft could be produced for this support question; send it again.")
        stats.answer_failures += 1
        return
    action = str(draft.get("suggested_action", "none"))[:40]
    store._add_attention(conn, _attention_id("support", event_id, "review"), "review", subject,
                         f"Draft reply, not sent (suggested action: {action}): {draft['answer_draft'][:500]}")
    stats.reviews += 1


def run_once(store, kind: str, batch_size: int = 20, failure_injection: bool = False,
             answer=None) -> Stats:
    """One bounded pass over one feed kind: one PostgreSQL transaction covers every effect and the cursor."""
    stats = Stats()
    name = cursor_name(kind)
    answer = answer or ask_support_service
    with store._connect() as conn:
        if kind == "support":
            store.purge_reviews(conn, review_retention_minutes())
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
            raw = {} if kind == "support" else item["payload"]  # support questions are never retained
            if isinstance(parsed, SupportPlan):
                _review_support(store, conn, stats, event_id, parsed, answer)
                parsed = None
                conn.execute("UPDATE ambient_events SET payload = '{}' WHERE seq = %s", (item["seq"],))
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
                store._quarantine_event(conn, parsed, raw)
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


def poll_pass(store, kind: str, batch_size: int, failure_injection: bool, answer=None) -> Stats:
    try:
        stats = run_once(store, kind, batch_size, failure_injection, answer)
    except Exception as exc:
        counters.by_kind[kind]["failures"] += 1
        log.warning("ambient %s poll failed: %s", kind, type(exc).__name__)
        raise
    counters.add(kind, stats)
    if stats.polled:
        log.info("ambient %s polled=%d created=%d duplicates=%d quarantined=%d questions=%d",
                 kind, stats.polled, stats.created, stats.duplicates, stats.quarantined, stats.questions)
    return stats
