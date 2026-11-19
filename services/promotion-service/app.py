from datetime import datetime

from pydantic import Field, field_validator

from parking_common.runtime import Actor, Admin, DomainError, Input, Internal, fail, make_app, now, remote, stamp

SCHEMA = """
CREATE TABLE IF NOT EXISTS promotions (
 code TEXT PRIMARY KEY, percent INTEGER NOT NULL CHECK(percent BETWEEN 1 AND 90),
 min_amount_cents INTEGER NOT NULL, max_uses INTEGER NOT NULL,
 lot_id TEXT, starts_at TEXT, ends_at TEXT, active INTEGER NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS claims (
 booking_id TEXT PRIMARY KEY, code TEXT NOT NULL REFERENCES promotions(code),
 user_id TEXT NOT NULL, lot_id TEXT NOT NULL, base_amount_cents INTEGER NOT NULL,
 discount_cents INTEGER NOT NULL, state TEXT NOT NULL CHECK(state IN ('HELD','USED','RELEASED')),
 expires_at TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS claims_code_state ON claims(code,state);
"""


class CodeInput(Input):
    code: str = Field(min_length=3, max_length=32)

    @field_validator("code")
    @classmethod
    def clean_code(cls, value):
        import re
        value = value.upper()
        if not re.fullmatch(r"[A-Z0-9_-]+", value):
            raise ValueError("Используйте латинские буквы, цифры, дефис или подчёркивание")
        return value


class PromotionInput(CodeInput):
    percent: int = Field(ge=1, le=90)
    min_amount_cents: int = Field(default=0, ge=0, le=2400000)
    max_uses: int = Field(default=100, ge=1, le=100000)
    lot_id: str | None = Field(default=None, max_length=50)
    starts_at: datetime | None = None
    ends_at: datetime | None = None


class StatusInput(Input):
    active: bool


class QuoteInput(CodeInput):
    lot_id: str = Field(min_length=1, max_length=50)
    base_amount_cents: int = Field(ge=1, le=2400000)


class ClaimInput(QuoteInput):
    booking_id: str = Field(min_length=1, max_length=50)
    user_id: str = Field(min_length=1, max_length=50)
    expires_at: datetime


def create_app(settings=None):
    def handle(c, event):
        kind, p = event["type"], event["payload"]
        if kind not in ("BookingConfirmed", "BookingCancelled", "BookingExpired"):
            return
        state = "USED" if kind == "BookingConfirmed" or p.get("was_confirmed") else "RELEASED"
        c.execute("UPDATE claims SET state=? WHERE booking_id=? AND state='HELD'", (state, p["booking_id"]))

    app = make_app("promotion", SCHEMA, settings, handle_event=handle)
    db = app.state.db
    with db.transaction() as c:
        c.execute("INSERT OR IGNORE INTO promotions VALUES('PARK10',10,0,100,NULL,NULL,NULL,1,?)", (stamp(),))

    def counts(c, code):
        return {r["state"]: r["n"] for r in c.execute(
            "SELECT state,COUNT(*) AS n FROM claims WHERE code=? GROUP BY state", (code,))}

    def describe(c, row):
        usage = counts(c, row["code"])
        return {**dict(row), "active": bool(row["active"]), "held": usage.get("HELD", 0),
                "used": usage.get("USED", 0)}

    def quote(c, body):
        row = c.execute("SELECT * FROM promotions WHERE code=?", (body.code,)).fetchone()
        clock = stamp()
        if (not row or not row["active"] or (row["starts_at"] and row["starts_at"] > clock)
                or (row["ends_at"] and row["ends_at"] <= clock)):
            fail(409, "PROMO_INACTIVE", "Промокод не найден или не действует")
        if row["lot_id"] and row["lot_id"] != body.lot_id:
            fail(409, "PROMO_LOT", "Промокод не действует на этой парковке")
        if body.base_amount_cents < row["min_amount_cents"]:
            fail(409, "PROMO_MINIMUM", "Стоимость ниже минимальной суммы промокода")
        usage = counts(c, body.code)
        if usage.get("HELD", 0) + usage.get("USED", 0) >= row["max_uses"]:
            fail(409, "PROMO_LIMIT", "Лимит применений промокода исчерпан")
        discount = body.base_amount_cents * row["percent"] // 100
        return {"code": body.code, "percent": row["percent"], "discount_cents": discount,
                "base_amount_cents": body.base_amount_cents, "amount_cents": body.base_amount_cents-discount}

    @app.get("/promotions", tags=["Промокоды"])
    def promotions(current: Admin):
        with db.read() as c:
            return [describe(c, row) for row in c.execute("SELECT * FROM promotions ORDER BY created_at DESC")]

    @app.post("/promotions", status_code=201, tags=["Промокоды"])
    def create(body: PromotionInput, current: Admin):
        for bound in (body.starts_at, body.ends_at):
            if bound and bound.utcoffset() is None:
                fail(422, "TIMEZONE_REQUIRED", "Укажите часовой пояс")
        if body.starts_at and body.ends_at and body.starts_at >= body.ends_at:
            fail(422, "INVALID_INTERVAL", "Окончание должно быть позже начала")
        if body.lot_id:
            remote(app, "parking", f"/parking/lots/{body.lot_id}")
        with db.transaction() as c:
            if c.execute("SELECT 1 FROM promotions WHERE code=?", (body.code,)).fetchone():
                fail(409, "PROMO_EXISTS", "Этот промокод уже существует")
            c.execute("INSERT INTO promotions VALUES(?,?,?,?,?,?,?,1,?)",
                      (body.code, body.percent, body.min_amount_cents, body.max_uses, body.lot_id,
                       stamp(body.starts_at) if body.starts_at else None,
                       stamp(body.ends_at) if body.ends_at else None, stamp()))
            return describe(c, c.execute("SELECT * FROM promotions WHERE code=?", (body.code,)).fetchone())

    @app.patch("/promotions/{code}", tags=["Промокоды"])
    def set_active(code: str, body: StatusInput, current: Admin):
        with db.transaction() as c:
            if not c.execute("UPDATE promotions SET active=? WHERE code=?", (int(body.active), code.upper())).rowcount:
                fail(404, "PROMO_NOT_FOUND", "Промокод не найден")
            return describe(c, c.execute("SELECT * FROM promotions WHERE code=?", (code.upper(),)).fetchone())

    @app.post("/promotions/quote", tags=["Промокоды"])
    def preview(body: QuoteInput, current: Actor):
        with db.read() as c:
            return quote(c, body)

    @app.post("/internal/promotions/claims", include_in_schema=False)
    def claim(body: ClaimInput, internal: Internal):
        if body.expires_at.utcoffset() is None or body.expires_at <= now():
            fail(422, "INVALID_EXPIRY", "Недопустимый срок резерва промокода")
        with db.transaction() as c:
            existing = c.execute("SELECT * FROM claims WHERE booking_id=?", (body.booking_id,)).fetchone()
            if existing:
                if any(existing[k] != getattr(body, k) for k in ("code","user_id","lot_id","base_amount_cents")):
                    fail(409, "PROMO_CONFLICT", "Данные применения промокода изменились")
                if existing["state"] == "USED":
                    fail(409, "PROMO_ALREADY_USED", "Скидка по этой брони уже использована")
                if existing["state"] == "HELD" and existing["expires_at"] > stamp():
                    return {"code": existing["code"], "discount_cents": existing["discount_cents"],
                            "base_amount_cents": existing["base_amount_cents"],
                            "amount_cents": existing["base_amount_cents"]-existing["discount_cents"],
                            "expires_at": existing["expires_at"]}
                if existing["state"] == "HELD":
                    fail(409, "PROMO_RECONCILING", "Резерв скидки истёк, повторите запрос позже")
            result = quote(c, body)
            expiry = stamp(body.expires_at)
            c.execute("""INSERT INTO claims VALUES(?,?,?,?,?,?,'HELD',?,?)
                       ON CONFLICT(booking_id) DO UPDATE SET discount_cents=excluded.discount_cents,
                       state='HELD',expires_at=excluded.expires_at""",
                      (body.booking_id, body.code, body.user_id, body.lot_id, body.base_amount_cents,
                       result["discount_cents"], expiry, stamp()))
            return {**result, "expires_at": expiry}

    def reconcile():
        # Ask the booking owner before freeing an expired hold. Its confirmation event
        # could still be in the outbox while the booking is already confirmed.
        with db.read() as c:
            pending = c.execute("SELECT booking_id,expires_at FROM claims WHERE state='HELD' AND expires_at<=? LIMIT 1",
                                (stamp(),)).fetchone()
        if not pending:
            return
        try:
            info = remote(app, "booking", f"/internal/bookings/promotion-state/{pending['booking_id']}", internal_call=True)
        except DomainError:
            return
        if info["state"] == "PENDING":
            return
        state = "USED" if info["state"] == "USED" else "RELEASED"
        with db.transaction() as c:
            c.execute("UPDATE claims SET state=? WHERE booking_id=? AND state='HELD' AND expires_at=?",
                      (state, pending["booking_id"], pending["expires_at"]))

    app.state.periodic = reconcile
    return app


if __name__ != "__unit__":
    app = create_app()
