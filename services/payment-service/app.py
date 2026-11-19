from fastapi import Request
from pydantic import Field
from typing import Literal

from parking_common.runtime import Actor, Admin, Input, Internal, emit, fail, make_app, remote, stamp, uid

SCHEMA = """
CREATE TABLE IF NOT EXISTS payments (
 id TEXT PRIMARY KEY, booking_id TEXT NOT NULL, user_id TEXT NOT NULL,
 amount_cents INTEGER NOT NULL CHECK(amount_cents>0), currency TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('SUCCEEDED','DECLINED','REFUNDED')),
 outcome TEXT NOT NULL, idempotency_key TEXT NOT NULL, created_at TEXT NOT NULL, refunded_at TEXT,
 UNIQUE(user_id,idempotency_key));
CREATE UNIQUE INDEX IF NOT EXISTS one_success_per_booking ON payments(booking_id) WHERE status='SUCCEEDED';
CREATE TABLE IF NOT EXISTS cancelled_bookings(booking_id TEXT PRIMARY KEY);
"""


class PaymentInput(Input):
    booking_id: str = Field(min_length=1, max_length=50)
    idempotency_key: str = Field(min_length=8, max_length=100)
    outcome: Literal["success", "decline"] = "success"


def snapshot(row):
    return {**{key: row[key] for key in ("id", "booking_id", "amount_cents", "currency", "status",
                                         "created_at", "refunded_at")},
            "updated_at": row["refunded_at"] or row["created_at"]}


def create_app(settings=None):
    def handle(c, event):
        if event["type"] not in ("BookingCancelled", "BookingExpired", "BookingPaymentRejected"):
            return
        p = event["payload"]
        # Remember cancellation even if the payment has not been inserted yet.
        c.execute("INSERT OR IGNORE INTO cancelled_bookings VALUES(?)", (p["booking_id"],))
        rows = c.execute("SELECT * FROM payments WHERE booking_id=? AND status='SUCCEEDED'", (p["booking_id"],)).fetchall()
        for row in rows:
            c.execute("UPDATE payments SET status='REFUNDED',refunded_at=? WHERE id=?", (stamp(), row["id"]))
            updated = c.execute("SELECT * FROM payments WHERE id=?", (row["id"],)).fetchone()
            emit(c, "PaymentRefunded", {"payment_id": row["id"], "booking_id": row["booking_id"],
                 "user_id": row["user_id"], "amount_cents": row["amount_cents"], "currency": row["currency"],
                 "payment": snapshot(updated)})

    app = make_app("payment", SCHEMA, settings, handle_event=handle)

    def validate_retry(row, body):
        if row["booking_id"] != body.booking_id or row["outcome"] != body.outcome:
            fail(409, "IDEMPOTENCY_CONFLICT", "Этот ключ уже использован для другого платежа")
        return dict(row)

    @app.post("/payments", status_code=201, tags=["Учебные платежи"])
    def pay(body: PaymentInput, request: Request, current: Actor):
        with app.state.db.read() as c:
            previous = c.execute("SELECT * FROM payments WHERE user_id=? AND idempotency_key=?",
                                 (current["sub"], body.idempotency_key)).fetchone()
        if previous:
            return validate_retry(previous, body)
        booking = remote(app, "booking", f"/bookings/{body.booking_id}", request)
        if booking["user_id"] != current["sub"]:
            fail(403, "FORBIDDEN", "Оплатить можно только своё бронирование")
        if booking["status"] != "PENDING":
            fail(409, "BOOKING_NOT_PAYABLE", "Бронирование уже оплачено, отменено или истекло")
        payment_id = uid()
        with app.state.db.transaction() as c:
            previous = c.execute("SELECT * FROM payments WHERE user_id=? AND idempotency_key=?",
                                 (current["sub"], body.idempotency_key)).fetchone()
            if previous:
                return validate_retry(previous, body)
            if c.execute("SELECT 1 FROM cancelled_bookings WHERE booking_id=?", (body.booking_id,)).fetchone():
                fail(409, "BOOKING_NOT_PAYABLE", "Бронирование отменено или истекло")
            if c.execute("SELECT 1 FROM payments WHERE booking_id=? AND status='SUCCEEDED'", (body.booking_id,)).fetchone():
                fail(409, "ALREADY_PAID", "Успешная оплата уже зарегистрирована")
            status = "SUCCEEDED" if body.outcome == "success" else "DECLINED"
            c.execute("INSERT INTO payments VALUES(?,?,?,?,?,?,?,?,?,NULL)",
                (payment_id, body.booking_id, current["sub"], booking["amount_cents"], booking["currency"],
                 status, body.outcome, body.idempotency_key, stamp()))
            row = c.execute("SELECT * FROM payments WHERE id=?", (payment_id,)).fetchone()
            emit(c, "PaymentSucceeded" if status == "SUCCEEDED" else "PaymentDeclined",
                 {"payment_id": payment_id, "booking_id": body.booking_id, "user_id": current["sub"],
                  "amount_cents": booking["amount_cents"], "currency": booking["currency"], "payment": snapshot(row)})
            return dict(row)

    @app.get("/payments", tags=["Учебные платежи"])
    def mine(current: Actor):
        with app.state.db.read() as c:
            return [dict(x) for x in c.execute("SELECT * FROM payments WHERE user_id=? ORDER BY created_at DESC LIMIT 200",
                                              (current["sub"],))]

    @app.get("/payments/admin/all", tags=["Администрирование"])
    def all_payments(current: Admin):
        with app.state.db.read() as c:
            return [dict(x) for x in c.execute("SELECT * FROM payments ORDER BY created_at DESC LIMIT 500")]

    @app.get("/internal/payments/reporting-snapshot", include_in_schema=False)
    def reporting_snapshot(internal: Internal, after_id: str = ""):
        with app.state.db.read() as c:
            return [snapshot(x) for x in c.execute("SELECT * FROM payments WHERE id>? ORDER BY id LIMIT 100", (after_id,))]

    return app


if __name__ != "__unit__":
    app = create_app()
