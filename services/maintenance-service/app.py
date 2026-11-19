from datetime import datetime, timedelta

from pydantic import Field

from parking_common.runtime import Admin, DomainError, Input, emit, fail, make_app, now, remote, stamp, uid

SCHEMA = """
CREATE TABLE IF NOT EXISTS work_windows (
 id TEXT PRIMARY KEY, space_id TEXT NOT NULL, lot_id TEXT, lot_name TEXT, space_code TEXT,
 start_at TEXT NOT NULL, end_at TEXT NOT NULL, reason TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('PENDING','ACTIVE','CANCELLING','CANCELLED','REJECTED')),
 idempotency_key TEXT UNIQUE NOT NULL, created_at TEXT NOT NULL,
 retry_at TEXT NOT NULL, error TEXT);
"""


class WindowInput(Input):
    space_id: str = Field(min_length=1, max_length=50)
    start_at: datetime
    end_at: datetime
    reason: str = Field(min_length=3, max_length=200)
    idempotency_key: str = Field(min_length=8, max_length=100)


def create_app(settings=None):
    app = make_app("maintenance", SCHEMA, settings)
    db = app.state.db

    def load(c, block_id):
        return c.execute("SELECT * FROM work_windows WHERE id=?", (block_id,)).fetchone()

    def describe(row):
        result = dict(row)
        if result["status"] == "ACTIVE" and result["end_at"] <= stamp():
            result["status"] = "COMPLETED"
        return result

    def activate(row):
        try:
            result = remote(app, "booking", "/internal/bookings/maintenance-blocks",
                method="POST", internal_call=True, json_body={"block_id": row["id"], "space_id": row["space_id"],
                 "start_at": row["start_at"], "end_at": row["end_at"]})
        except DomainError as exc:
            with db.transaction() as c:
                terminal = exc.status in (404,409,422)
                c.execute("UPDATE work_windows SET status=?,error=?,retry_at=? WHERE id=? AND status='PENDING'",
                    ("REJECTED" if terminal else "PENDING", exc.message, stamp(now()+timedelta(seconds=5)), row["id"]))
            raise
        with db.transaction() as c:
            if c.execute("""UPDATE work_windows SET status='ACTIVE',lot_id=?,lot_name=?,space_code=?,error=NULL
                             WHERE id=? AND status='PENDING'""",
                         (result["lot_id"],result["lot_name"],result["space_code"],row["id"])).rowcount:
                emit(c, "MaintenanceScheduled", {"maintenance_id": row["id"], "space_id": row["space_id"]})
            return describe(load(c, row["id"]))

    def release(row):
        try:
            remote(app, "booking", f"/internal/bookings/maintenance-blocks/{row['id']}",
                   method="DELETE", internal_call=True)
        except DomainError as exc:
            with db.transaction() as c:
                c.execute("UPDATE work_windows SET error=?,retry_at=? WHERE id=? AND status='CANCELLING'",
                          (exc.message, stamp(now()+timedelta(seconds=5)),row["id"]))
            raise
        with db.transaction() as c:
            if c.execute("UPDATE work_windows SET status='CANCELLED',error=NULL WHERE id=? AND status='CANCELLING'",
                         (row["id"],)).rowcount:
                emit(c, "MaintenanceCancelled", {"maintenance_id": row["id"], "space_id": row["space_id"]})
            return describe(load(c, row["id"]))

    def reconcile():
        with db.read() as c:
            row = c.execute("""SELECT * FROM work_windows WHERE status IN ('PENDING','CANCELLING')
                             AND retry_at<=? ORDER BY retry_at LIMIT 1""", (stamp(),)).fetchone()
        if row:
            try:
                (activate if row["status"] == "PENDING" else release)(row)
            except DomainError:
                pass

    app.state.periodic = reconcile

    @app.get("/maintenance", tags=["Обслуживание"])
    def list_windows(current: Admin):
        with db.read() as c:
            return [describe(r) for r in c.execute("SELECT * FROM work_windows ORDER BY created_at DESC LIMIT 200")]

    @app.post("/maintenance", status_code=201, tags=["Обслуживание"])
    def create(body: WindowInput, current: Admin):
        if body.start_at.utcoffset() is None or body.end_at.utcoffset() is None:
            fail(422, "TIMEZONE_REQUIRED", "Укажите часовой пояс")
        start, end = stamp(body.start_at), stamp(body.end_at)
        with db.transaction() as c:
            row = c.execute("SELECT * FROM work_windows WHERE idempotency_key=?", (body.idempotency_key,)).fetchone()
            if row:
                if (row["space_id"],row["start_at"],row["end_at"],row["reason"]) != (body.space_id,start,end,body.reason):
                    fail(409, "IDEMPOTENCY_CONFLICT", "Этот ключ уже использован для другого интервала")
                if row["status"] == "REJECTED":
                    fail(409, "MAINTENANCE_REJECTED", row["error"])
                if row["status"] != "PENDING":
                    return describe(row)
            else:
                if body.start_at <= now() or not timedelta(minutes=1) <= body.end_at-body.start_at <= timedelta(days=30):
                    fail(422, "INVALID_INTERVAL", "Начало в будущем, длительность от минуты до 30 дней")
                block_id = uid()
                c.execute("""INSERT INTO work_windows(id,space_id,start_at,end_at,reason,status,idempotency_key,created_at,retry_at)
                           VALUES(?,?,?,?,?,'PENDING',?,?,?)""",
                          (block_id, body.space_id, start, end, body.reason, body.idempotency_key, stamp(),
                           stamp(now()+timedelta(seconds=5))))
                row = load(c, block_id)
        return activate(row)

    @app.post("/maintenance/{block_id}/cancel", tags=["Обслуживание"])
    def cancel(block_id: str, current: Admin):
        with db.transaction() as c:
            row = load(c, block_id)
            if not row:
                fail(404, "MAINTENANCE_NOT_FOUND", "Интервал обслуживания не найден")
            if row["status"] == "CANCELLED":
                return describe(row)
            if row["status"] not in ("ACTIVE","CANCELLING"):
                fail(409, "MAINTENANCE_STATE", "Дождитесь синхронизации или выберите активный интервал")
            c.execute("UPDATE work_windows SET status='CANCELLING',retry_at=? WHERE id=?",
                      (stamp(now()+timedelta(seconds=5)),block_id))
        return release(row)

    return app


if __name__ != "__unit__":
    app = create_app()
