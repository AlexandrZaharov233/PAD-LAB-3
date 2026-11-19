import hashlib
import uuid
from datetime import datetime, timedelta
from urllib.parse import quote

from fastapi import Request
from pydantic import Field

from parking_common.runtime import (Actor, Admin, Input, Internal, emit, fail, interval,
                                    make_app, now, remote, stamp, uid)

SCHEMA = """
CREATE TABLE IF NOT EXISTS bookings (
 id TEXT PRIMARY KEY, user_id TEXT NOT NULL, space_id TEXT NOT NULL, lot_id TEXT NOT NULL,
 lot_name TEXT NOT NULL, space_code TEXT NOT NULL, vehicle_id TEXT NOT NULL, plate TEXT NOT NULL,
 start_at TEXT NOT NULL, end_at TEXT NOT NULL,
 amount_cents INTEGER NOT NULL CHECK(amount_cents>0), currency TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('PENDING','CONFIRMED','CANCELLED','EXPIRED')),
 expires_at TEXT NOT NULL, payment_id TEXT, created_at TEXT NOT NULL,
 idempotency_key TEXT NOT NULL, request_hash TEXT NOT NULL, UNIQUE(user_id,idempotency_key));
CREATE INDEX IF NOT EXISTS bookings_space_time ON bookings(space_id,start_at,end_at,status);
CREATE TABLE IF NOT EXISTS maintenance_blocks (
 id TEXT PRIMARY KEY, space_id TEXT NOT NULL, lot_id TEXT NOT NULL, lot_name TEXT NOT NULL,
 space_code TEXT NOT NULL, start_at TEXT NOT NULL, end_at TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1);
CREATE INDEX IF NOT EXISTS maintenance_space_time ON maintenance_blocks(space_id,start_at,end_at,active);
"""


class BookingInput(Input):
    space_id: str = Field(min_length=1, max_length=50)
    vehicle_id: str = Field(min_length=1, max_length=50)
    start_at: datetime
    end_at: datetime
    idempotency_key: str = Field(min_length=8, max_length=100)
    promo_code: str | None = Field(default=None, min_length=3, max_length=32)


class CalendarBlock(Input):
    block_id: str = Field(min_length=1, max_length=50)
    space_id: str = Field(min_length=1, max_length=50)
    start_at: datetime
    end_at: datetime


def snapshot(row):
    return {key: row[key] for key in ("id", "lot_id", "lot_name", "space_id", "space_code", "start_at",
            "end_at", "status", "amount_cents", "discount_cents", "currency", "created_at", "updated_at")}


def booking_event(c, kind, row, **extra):
    payload = {key: row[key] for key in ("user_id", "lot_id", "lot_name", "space_id", "space_code", "start_at", "end_at")}
    emit(c, kind, {**payload, **extra, "booking_id": row["id"], "booking": snapshot(row)})


def release_slot(c, row):
    emit(c, "ParkingSlotReleased", {key: row[key] for key in
         ("lot_id", "lot_name", "space_id", "space_code", "start_at", "end_at")})


def create_app(settings=None):
    app = make_app("booking", SCHEMA, settings)
    # Additive migration: existing v1.0 SQLite data remains in the same volume.
    with app.state.db.transaction() as c:
        cols = {r["name"] for r in c.execute("PRAGMA table_info(bookings)")}
        for name, definition in (("base_amount_cents", "INTEGER"), ("discount_cents", "INTEGER NOT NULL DEFAULT 0"),
                                 ("promo_code", "TEXT"), ("updated_at", "TEXT")):
            if name not in cols:
                c.execute(f"ALTER TABLE bookings ADD COLUMN {name} {definition}")
        c.execute("UPDATE bookings SET base_amount_cents=amount_cents WHERE base_amount_cents IS NULL")
        c.execute("UPDATE bookings SET updated_at=created_at WHERE updated_at IS NULL")

    def load(c, booking_id):
        return c.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()

    def expire(c):
        rows = c.execute("SELECT * FROM bookings WHERE status='PENDING' AND expires_at<=?", (stamp(),)).fetchall()
        for row in rows:
            c.execute("UPDATE bookings SET status='EXPIRED',updated_at=? WHERE id=?", (stamp(), row["id"]))
            booking_event(c, "BookingExpired", load(c, row["id"]))
            release_slot(c, row)

    def periodic():
        with app.state.db.transaction() as c:
            expire(c)

    def handle(c, event):
        if event["type"] != "PaymentSucceeded":
            return
        p = event["payload"]
        expire(c)
        row = load(c, p["booking_id"])
        if row and row["status"] == "CONFIRMED" and row["payment_id"] == p["payment_id"]:
            return
        if (row and row["status"] == "PENDING" and row["user_id"] == p["user_id"]
                and row["amount_cents"] == p["amount_cents"] and row["currency"] == p["currency"]):
            c.execute("UPDATE bookings SET status='CONFIRMED',payment_id=?,updated_at=? WHERE id=?",
                      (p["payment_id"], stamp(), row["id"]))
            booking_event(c, "BookingConfirmed", load(c, row["id"]))
        else:
            # Compensation for a payment arriving after cancellation/expiration.
            emit(c, "BookingPaymentRejected", {"booking_id": p["booking_id"],
                 "payment_id": p["payment_id"], "user_id": p["user_id"]})

    app.state.handle_event = handle
    # make_app creates the worker on startup; expose the timer via a small factory hook.
    app.state.periodic = periodic

    def get_owned(booking_id, current):
        periodic()
        with app.state.db.read() as c:
            row = c.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
            if not row or (row["user_id"] != current["sub"] and current["role"] != "admin"):
                fail(404, "BOOKING_NOT_FOUND", "Бронирование не найдено")
            return dict(row)

    @app.get("/bookings/availability", tags=["Доступность"])
    def availability(lot_id: str, start_at: datetime, end_at: datetime, request: Request):
        start, end, hours = interval(start_at, end_at)
        safe_lot = quote(lot_id, safe="")
        lot = remote(app, "parking", f"/parking/lots/{safe_lot}")
        spaces = remote(app, "parking", f"/parking/lots/{safe_lot}/spaces")
        periodic()
        with app.state.db.read() as c:
            occupied = {r[0] for r in c.execute("""SELECT space_id FROM bookings WHERE lot_id=?
               AND status IN ('PENDING','CONFIRMED') AND start_at<? AND end_at>?""", (lot_id, end, start))}
            maintenance = {r[0] for r in c.execute("""SELECT space_id FROM maintenance_blocks WHERE lot_id=?
               AND active=1 AND start_at<? AND end_at>?""", (lot_id, end, start))}
        return {"lot": lot, "start_at": start, "end_at": end,
                "amount_cents": hours*lot["hourly_rate_cents"], "currency": lot["currency"],
                "spaces": [{**x, "maintenance": x["id"] in maintenance,
                            "available": bool(lot["active"] and x["active"] and x["id"] not in occupied
                                              and x["id"] not in maintenance)} for x in spaces]}

    @app.post("/bookings", status_code=201, tags=["Бронирования"])
    def create(body: BookingInput, request: Request, current: Actor):
        fingerprint = hashlib.sha256(body.model_dump_json(exclude={"idempotency_key"}, exclude_none=True).encode()).hexdigest()
        # A completed retry succeeds even if start time has passed or a dependency is unavailable.
        with app.state.db.read() as c:
            previous = c.execute("SELECT * FROM bookings WHERE user_id=? AND idempotency_key=?",
                                 (current["sub"], body.idempotency_key)).fetchone()
        if previous:
            if previous["request_hash"] != fingerprint:
                fail(409, "IDEMPOTENCY_CONFLICT", "Этот ключ уже использован для другого запроса")
            return dict(previous)
        start, end, hours = interval(body.start_at, body.end_at)
        space = remote(app, "parking", f"/parking/spaces/{quote(body.space_id, safe='')}")
        vehicle = remote(app, "profile", f"/profiles/me/vehicles/{quote(body.vehicle_id, safe='')}", request)
        if not space["active"] or not space["lot_active"]:
            fail(409, "SPACE_DISABLED", "Место временно недоступно")
        # A retry after an uncertain promotion response reuses the claim.
        booking_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"parkslot:{current['sub']}:{body.idempotency_key}:{fingerprint}"))
        with app.state.db.transaction() as c:
            expire(c)
            # Repeat inside the write lock to handle concurrent retries correctly.
            previous = c.execute("SELECT * FROM bookings WHERE user_id=? AND idempotency_key=?",
                                 (current["sub"], body.idempotency_key)).fetchone()
            if previous:
                if previous["request_hash"] != fingerprint:
                    fail(409, "IDEMPOTENCY_CONFLICT", "Этот ключ уже использован для другого запроса")
                return dict(previous)
            if c.execute("""SELECT 1 FROM bookings WHERE space_id=? AND status IN ('PENDING','CONFIRMED')
                          AND start_at<? AND end_at>?""", (body.space_id, end, start)).fetchone():
                fail(409, "SPACE_OCCUPIED", "Это место уже забронировано на выбранное время")
            if c.execute("""SELECT 1 FROM maintenance_blocks WHERE space_id=? AND active=1
                          AND start_at<? AND end_at>?""", (body.space_id, end, start)).fetchone():
                fail(409, "SPACE_MAINTENANCE", "Место закрыто на обслуживание на выбранное время")
            base = hours * space["hourly_rate_cents"]
            discount, promo_code = 0, None
            expiry = stamp(now()+timedelta(minutes=10))
            if body.promo_code:
                # Promotion's claim endpoint does not call Booking.
                claim = remote(app, "promotion", "/internal/promotions/claims", method="POST", internal_call=True,
                    json_body={"booking_id": booking_id, "user_id": current["sub"], "code": body.promo_code,
                               "lot_id": space["lot_id"], "base_amount_cents": base, "expires_at": expiry})
                discount, promo_code, expiry = claim["discount_cents"], claim["code"], claim["expires_at"]
            created = stamp()
            c.execute("""INSERT INTO bookings
                (id,user_id,space_id,lot_id,lot_name,space_code,vehicle_id,plate,start_at,end_at,amount_cents,
                 currency,status,expires_at,payment_id,created_at,idempotency_key,request_hash,
                 base_amount_cents,discount_cents,promo_code,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (booking_id, current["sub"], body.space_id, space["lot_id"], space["lot_name"], space["code"],
                 body.vehicle_id, vehicle["plate"], start, end, base-discount, space["currency"], "PENDING",
                 expiry, None, created, body.idempotency_key, fingerprint, base, discount, promo_code, created))
            result = load(c, booking_id)
            booking_event(c, "BookingCreated", result)
            return dict(result)

    @app.get("/bookings", tags=["Бронирования"])
    def mine(current: Actor):
        periodic()
        with app.state.db.read() as c:
            return [dict(x) for x in c.execute("SELECT * FROM bookings WHERE user_id=? ORDER BY created_at DESC LIMIT 200",
                                              (current["sub"],))]

    @app.get("/bookings/admin/all", tags=["Администрирование"])
    def all_bookings(current: Admin):
        periodic()
        with app.state.db.read() as c:
            return [dict(x) for x in c.execute("SELECT * FROM bookings ORDER BY created_at DESC LIMIT 500")]

    @app.get("/bookings/{booking_id}", tags=["Бронирования"])
    def detail(booking_id: str, current: Actor):
        return get_owned(booking_id, current)

    @app.post("/bookings/{booking_id}/cancel", tags=["Бронирования"])
    def cancel(booking_id: str, current: Actor):
        with app.state.db.transaction() as c:
            expire(c)
            row = c.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
            if not row or (row["user_id"] != current["sub"] and current["role"] != "admin"):
                fail(404, "BOOKING_NOT_FOUND", "Бронирование не найдено")
            if row["status"] in ("CANCELLED", "EXPIRED"):
                return dict(row)
            if row["start_at"] <= stamp() and current["role"] != "admin":
                fail(409, "BOOKING_STARTED", "Нельзя отменить уже начавшуюся парковку")
            c.execute("UPDATE bookings SET status='CANCELLED',updated_at=? WHERE id=?", (stamp(), booking_id))
            result = load(c, booking_id)
            booking_event(c, "BookingCancelled", result, was_confirmed=row["status"] == "CONFIRMED")
            release_slot(c, row)
            return dict(result)

    @app.get("/internal/bookings/promotion-state/{booking_id}", include_in_schema=False)
    def promotion_state(booking_id: str, internal: Internal):
        with app.state.db.transaction() as c:
            expire(c)
            row = load(c, booking_id)
            state = "MISSING" if not row else ("USED" if row["payment_id"] else row["status"])
            return {"state": state}

    @app.get("/internal/bookings/reporting-snapshot", include_in_schema=False)
    def reporting_snapshot(internal: Internal, after_id: str = ""):
        with app.state.db.read() as c:
            return [snapshot(row) for row in c.execute("SELECT * FROM bookings WHERE id>? ORDER BY id LIMIT 100", (after_id,))]

    @app.post("/internal/bookings/maintenance-blocks", include_in_schema=False)
    def put_block(body: CalendarBlock, internal: Internal):
        if body.start_at.utcoffset() is None or body.end_at.utcoffset() is None:
            fail(422, "TIMEZONE_REQUIRED", "Укажите часовой пояс обслуживания")
        start, end = stamp(body.start_at), stamp(body.end_at)

        def existing_block(c):
            row = c.execute("SELECT * FROM maintenance_blocks WHERE id=?", (body.block_id,)).fetchone()
            if not row:
                return None
            if not row["active"] or (row["space_id"], row["start_at"], row["end_at"]) != (body.space_id, start, end):
                fail(409, "MAINTENANCE_CONFLICT", "Идентификатор интервала уже использован")
            return dict(row)

        with app.state.db.read() as c:
            existing = existing_block(c)
        if existing:
            return existing
        if body.start_at <= now() or not timedelta(minutes=1) <= body.end_at-body.start_at <= timedelta(days=30):
            fail(422, "INVALID_INTERVAL", "Начало в будущем, длительность от минуты до 30 дней")
        space = remote(app, "parking", f"/parking/spaces/{quote(body.space_id, safe='')}")
        with app.state.db.transaction() as c:
            existing = existing_block(c)
            if existing:
                return existing
            expire(c)
            if c.execute("""SELECT 1 FROM bookings WHERE space_id=? AND status IN ('PENDING','CONFIRMED')
                         AND start_at<? AND end_at>?""", (body.space_id, end, start)).fetchone():
                fail(409, "MAINTENANCE_BOOKED", "На это время уже есть бронь. Выберите другой интервал")
            if c.execute("""SELECT 1 FROM maintenance_blocks WHERE space_id=? AND active=1
                         AND start_at<? AND end_at>?""", (body.space_id, end, start)).fetchone():
                fail(409, "MAINTENANCE_OVERLAP", "На это время уже запланировано обслуживание")
            c.execute("INSERT INTO maintenance_blocks VALUES(?,?,?,?,?,?,?,1)",
                      (body.block_id, body.space_id, space["lot_id"], space["lot_name"], space["code"], start, end))
            return existing_block(c)

    @app.delete("/internal/bookings/maintenance-blocks/{block_id}", include_in_schema=False)
    def remove_block(block_id: str, internal: Internal):
        with app.state.db.transaction() as c:
            row = c.execute("SELECT * FROM maintenance_blocks WHERE id=?", (block_id,)).fetchone()
            if row and row["active"]:
                c.execute("UPDATE maintenance_blocks SET active=0 WHERE id=?", (block_id,))
                release_slot(c, row)
        return {"released": True}

    return app


if __name__ != "__unit__":
    app = create_app()
