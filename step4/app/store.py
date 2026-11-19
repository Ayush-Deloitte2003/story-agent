import json
import os

import psycopg
from psycopg.rows import dict_row


class PostgresStore:
    def __init__(self, dsn: str | None = None):
        self.dsn = dsn

    def _connect(self):
        return psycopg.connect(self.dsn or os.environ["DATABASE_URL"], row_factory=dict_row)

    def ping(self) -> None:
        with self._connect() as conn:
            conn.execute("SELECT 1")

    def initialize(self) -> None:
        with self._connect() as conn:
            ddl = """
                CREATE TABLE IF NOT EXISTS flight_inventory (
                    flight_id TEXT PRIMARY KEY, origin TEXT NOT NULL, destination TEXT NOT NULL,
                    depart_at TEXT NOT NULL, seats JSONB NOT NULL, price_cents INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS bookings (
                    booking_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
                    request JSONB NOT NULL, status TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                );
                CREATE TABLE IF NOT EXISTS seat_holds (
                    booking_id TEXT PRIMARY KEY REFERENCES bookings(booking_id),
                    flight_id TEXT NOT NULL REFERENCES flight_inventory(flight_id),
                    seat TEXT NOT NULL, expires_at TIMESTAMPTZ NOT NULL, status TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_booking_per_seat
                    ON seat_holds(flight_id, seat);
                CREATE TABLE IF NOT EXISTS simulated_payments (
                    booking_id TEXT PRIMARY KEY REFERENCES bookings(booking_id),
                    payment_id TEXT NOT NULL UNIQUE, amount_cents INTEGER NOT NULL, status TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tickets (
                    booking_id TEXT PRIMARY KEY REFERENCES bookings(booking_id),
                    ticket_id TEXT NOT NULL UNIQUE, flight_id TEXT NOT NULL, seat TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS approval_inbox (
                    workflow_id TEXT PRIMARY KEY, kind TEXT NOT NULL, booking_id TEXT NOT NULL,
                    status TEXT NOT NULL, expires_at TIMESTAMPTZ NOT NULL,
                    proposal JSONB NOT NULL, decision JSONB
                );
            """
            for statement in ddl.split(";"):
                if statement.strip():
                    conn.execute(statement)
            for flight_id, flight in self._mock_flights().items():
                conn.execute(
                    """INSERT INTO flight_inventory(flight_id, origin, destination, depart_at, seats, price_cents)
                       VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (flight_id) DO NOTHING""",
                    (flight_id, flight["origin"], flight["destination"], flight["depart_at"],
                     json.dumps(flight["seats"]), flight["price_cents"]),
                )

    @staticmethod
    def _mock_flights() -> dict:
        from app.inventory import FLIGHTS
        return FLIGHTS

    def create_booking(self, workflow_id: str, idempotency_key: str, request: dict) -> tuple[dict, bool]:
        with self._connect() as conn:
            row = conn.execute(
                """INSERT INTO bookings(booking_id, idempotency_key, request, status)
                   VALUES (%s, %s, %s, 'scheduled')
                   ON CONFLICT (idempotency_key) DO NOTHING RETURNING *""",
                (workflow_id, idempotency_key, json.dumps(request)),
            ).fetchone()
            if row:
                return row, True
            row = conn.execute(
                "SELECT * FROM bookings WHERE idempotency_key = %s", (idempotency_key,)
            ).fetchone()
            if row["request"] != request:
                raise ValueError("Idempotency key was already used for a different booking")
            return row, False

    def booking(self, booking_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM bookings WHERE booking_id = %s", (booking_id,)).fetchone()
            if row is None:
                return None
            row["request"] = self._json_value(row["request"])
            hold = conn.execute("SELECT * FROM seat_holds WHERE booking_id = %s", (booking_id,)).fetchone()
            row["seat_hold"] = dict(hold) if hold else None
            payment = conn.execute("SELECT * FROM simulated_payments WHERE booking_id = %s", (booking_id,)).fetchone()
            row["payment"] = dict(payment) if payment else None
            ticket = conn.execute("SELECT * FROM tickets WHERE booking_id = %s", (booking_id,)).fetchone()
            row["ticket"] = dict(ticket) if ticket else None
            return row

    @staticmethod
    def _json_value(value):
        return json.loads(value) if isinstance(value, str) else value

    def reserve_seat(self, booking_id: str, flight_id: str, seat: str, hold_seconds: int) -> bool:
        with self._connect() as conn:
            conn.execute("DELETE FROM seat_holds WHERE status = 'held' AND expires_at <= now()")
            valid = conn.execute(
                "SELECT seats FROM flight_inventory WHERE flight_id = %s", (flight_id,)
            ).fetchone()
            if not valid or seat not in self._json_value(valid["seats"]):
                return False
            row = conn.execute(
                """INSERT INTO seat_holds(booking_id, flight_id, seat, expires_at, status)
                   VALUES (%s, %s, %s, now() + (%s * interval '1 second'), 'held')
                   ON CONFLICT DO NOTHING RETURNING booking_id""",
                (booking_id, flight_id, seat, hold_seconds),
            ).fetchone()
            if row:
                conn.execute("UPDATE bookings SET status = 'held' WHERE booking_id = %s", (booking_id,))
                return True
            existing = conn.execute(
                """SELECT 1 FROM seat_holds WHERE booking_id = %s AND flight_id = %s
                   AND seat = %s AND status = 'held' AND expires_at > now()""",
                (booking_id, flight_id, seat),
            ).fetchone()
            return existing is not None

    def create_booking_approval(self, workflow_id: str, booking_id: str, expires_at, proposal: dict) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO approval_inbox(workflow_id, kind, booking_id, status, expires_at, proposal)
                   VALUES (%s, 'booking', %s, 'pending', %s, %s)
                   ON CONFLICT (workflow_id) DO NOTHING""",
                (workflow_id, booking_id, expires_at, json.dumps(proposal)),
            )
            conn.execute("UPDATE bookings SET status = 'awaiting_approval' WHERE booking_id = %s", (booking_id,))

    def decide(self, workflow_id: str, decision: dict) -> bool:
        with self._connect() as conn:
            updated = conn.execute(
                """UPDATE approval_inbox SET status = %s, decision = %s
                   WHERE workflow_id = %s AND status = 'pending' AND expires_at > now()
                   RETURNING workflow_id""",
                ("approved" if decision["decision"] == "approve" else "rejected",
                 json.dumps(decision), workflow_id),
            ).fetchone()
            if updated:
                return True
            row = conn.execute(
                "SELECT decision FROM approval_inbox WHERE workflow_id = %s", (workflow_id,)
            ).fetchone()
            return bool(row and self._json_value(row["decision"]) == decision)

    def inbox_item(self, workflow_id: str) -> dict | None:
        with self._connect() as conn:
            item = conn.execute("SELECT * FROM approval_inbox WHERE workflow_id = %s", (workflow_id,)).fetchone()
            if item:
                item["proposal"] = self._json_value(item["proposal"])
                item["decision"] = self._json_value(item["decision"])
            return item

    def inbox(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM approval_inbox WHERE status = 'pending' ORDER BY expires_at"
            ).fetchall()
            for row in rows:
                row["proposal"] = self._json_value(row["proposal"])
            return rows

    def mark_booking(self, booking_id: str, status: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE bookings SET status = %s WHERE booking_id = %s", (status, booking_id))

    def simulate_payment(self, booking_id: str, amount_cents: int, failure_at: str | None) -> dict:
        if failure_at == "payment":
            raise RuntimeError("Injected simulated payment failure")
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT * FROM simulated_payments WHERE booking_id = %s", (booking_id,)
            ).fetchone()
            if existing:
                return existing
            eligible = conn.execute(
                """SELECT 1 FROM seat_holds h JOIN approval_inbox i
                   ON i.booking_id = h.booking_id
                   WHERE h.booking_id = %s AND h.status = 'held' AND h.expires_at > now()
                   AND i.kind = 'booking' AND i.status = 'approved'""",
                (booking_id,),
            ).fetchone()
            if not eligible:
                raise RuntimeError("Payment requires an approved booking with an active seat hold")
            row = conn.execute(
                """INSERT INTO simulated_payments(booking_id, payment_id, amount_cents, status)
                   VALUES (%s, %s, %s, 'paid') ON CONFLICT (booking_id) DO NOTHING RETURNING *""",
                (booking_id, f"pay-{booking_id}", amount_cents),
            ).fetchone()
            if row:
                return row
            return conn.execute(
                "SELECT * FROM simulated_payments WHERE booking_id = %s", (booking_id,)
            ).fetchone()

    def issue_ticket(self, booking_id: str, flight_id: str, seat: str, failure_at: str | None) -> dict:
        if failure_at == "ticketing":
            raise RuntimeError("Injected simulated ticketing failure")
        with self._connect() as conn:
            hold = conn.execute(
                """SELECT h.status, p.status AS payment_status FROM seat_holds h
                   JOIN simulated_payments p ON p.booking_id = h.booking_id
                   WHERE h.booking_id = %s""",
                (booking_id,),
            ).fetchone()
            if not hold or hold["payment_status"] != "paid" or hold["status"] not in ("held", "confirmed"):
                raise RuntimeError("Ticket issuance requires a paid booking and seat hold")
            ticket = conn.execute(
                """INSERT INTO tickets(booking_id, ticket_id, flight_id, seat)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT (booking_id) DO UPDATE SET flight_id = EXCLUDED.flight_id,
                     seat = EXCLUDED.seat RETURNING *""",
                (booking_id, f"ticket-{booking_id}", flight_id, seat),
            ).fetchone()
            conn.execute("UPDATE seat_holds SET status = 'confirmed' WHERE booking_id = %s", (booking_id,))
            conn.execute("UPDATE bookings SET status = 'confirmed' WHERE booking_id = %s", (booking_id,))
            return ticket

    def compensate(self, booking_id: str, workflow_id: str, reason: str) -> dict:
        inbox_status = reason if reason in ("expired", "rejected", "failed") else "failed"
        with self._connect() as conn:
            conn.execute(
                "UPDATE simulated_payments SET status = 'refunded' WHERE booking_id = %s AND status = 'paid'",
                (booking_id,),
            )
            conn.execute("DELETE FROM seat_holds WHERE booking_id = %s AND status = 'held'", (booking_id,))
            conn.execute("UPDATE bookings SET status = 'compensated' WHERE booking_id = %s", (booking_id,))
            conn.execute(
                """UPDATE approval_inbox SET status = %s WHERE workflow_id = %s
                   AND status IN ('pending', 'approved')""",
                (inbox_status, workflow_id),
            )
        return {"booking_id": booking_id, "status": "compensated"}

    def create_rebooking_approval(self, workflow_id: str, booking_id: str, expires_at, proposal: dict) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO approval_inbox(workflow_id, kind, booking_id, status, expires_at, proposal)
                   VALUES (%s, 'rebooking', %s, 'pending', %s, %s)
                   ON CONFLICT (workflow_id) DO NOTHING""",
                (workflow_id, booking_id, expires_at, json.dumps(proposal)),
            )

    def rebook(self, booking_id: str, workflow_id: str, flight_id: str) -> dict:
        with self._connect() as conn:
            conn.execute("DELETE FROM seat_holds WHERE status = 'held' AND expires_at <= now()")
            completed = conn.execute(
                """SELECT i.status AS inbox_status, t.* FROM approval_inbox i
                   JOIN tickets t ON t.booking_id = i.booking_id
                   WHERE i.workflow_id = %s AND i.booking_id = %s""",
                (workflow_id, booking_id),
            ).fetchone()
            if completed and completed["inbox_status"] == "completed" and completed["flight_id"] == flight_id:
                ticket = {key: completed[key] for key in ("booking_id", "ticket_id", "flight_id", "seat")}
                return {"booking_id": booking_id, "status": "confirmed", "ticket": ticket}
            booking = conn.execute(
                """SELECT b.status FROM bookings b
                   JOIN seat_holds h ON h.booking_id = b.booking_id WHERE b.booking_id = %s""",
                (booking_id,),
            ).fetchone()
            flight = conn.execute("SELECT seats FROM flight_inventory WHERE flight_id = %s", (flight_id,)).fetchone()
            if not booking or booking["status"] != "confirmed":
                raise RuntimeError("Only confirmed bookings can be rebooked")
            if not flight:
                raise RuntimeError("Recommended flight or seat is not in mock inventory")
            seats = self._json_value(flight["seats"])
            seat = None
            for candidate in seats:
                taken = conn.execute(
                    "SELECT 1 FROM seat_holds WHERE flight_id = %s AND seat = %s AND booking_id <> %s",
                    (flight_id, candidate, booking_id),
                ).fetchone()
                if not taken:
                    seat = candidate
                    break
            if not seat:
                raise RuntimeError("No mock seats remain on the approved alternative")
            try:
                conn.execute(
                    """UPDATE seat_holds SET flight_id = %s, seat = %s, status = 'confirmed',
                       expires_at = now() WHERE booking_id = %s""",
                    (flight_id, seat, booking_id),
                )
            except psycopg.errors.UniqueViolation as exc:
                raise RuntimeError("Recommended seat is no longer available") from exc
            conn.execute(
                "UPDATE tickets SET flight_id = %s, seat = %s WHERE booking_id = %s",
                (flight_id, seat, booking_id),
            )
            conn.execute("UPDATE bookings SET status = 'confirmed' WHERE booking_id = %s", (booking_id,))
            conn.execute("UPDATE approval_inbox SET status = 'completed' WHERE workflow_id = %s", (workflow_id,))
            ticket = conn.execute("SELECT * FROM tickets WHERE booking_id = %s", (booking_id,)).fetchone()
            return {"booking_id": booking_id, "status": "confirmed", "ticket": ticket}

    def close_rebooking(self, workflow_id: str, status: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """UPDATE approval_inbox SET status = %s WHERE workflow_id = %s
                   AND status IN ('pending', 'approved')""",
                (status, workflow_id),
            )
