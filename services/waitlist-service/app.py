from datetime import datetime, timedelta
from urllib.parse import urlencode

from pydantic import Field

from parking_common.runtime import Actor, DomainError, Input, emit, fail, interval, make_app, now, remote, stamp, uid

SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
 id TEXT PRIMARY KEY, user_id TEXT NOT NULL, lot_id TEXT NOT NULL, lot_name TEXT NOT NULL,
 start_at TEXT NOT NULL, end_at TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('WAITING','OFFERED','FULFILLED','CANCELLED','EXPIRED')),
 offered_space_id TEXT, space_code TEXT, offer_until TEXT, created_at TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS active_waitlist_entry
 ON entries(user_id,lot_id,start_at,end_at) WHERE status IN ('WAITING','OFFERED');
CREATE INDEX IF NOT EXISTS waitlist_match ON entries(lot_id,status,start_at,end_at,created_at);
CREATE TABLE IF NOT EXISTS jobs (
 id TEXT PRIMARY KEY, lot_id TEXT NOT NULL, space_id TEXT, start_at TEXT NOT NULL,
 end_at TEXT NOT NULL, created_at TEXT NOT NULL);
"""


class WaitInput(Input):
    lot_id: str = Field(min_length=1, max_length=50)
    start_at: datetime
    end_at: datetime


def enqueue(c, lot, space, start, end, job_id=None):
    c.execute("INSERT OR IGNORE INTO jobs VALUES(?,?,?,?,?,?)", (job_id or uid(),lot,space,start,end,stamp()))


def expire(c):
    clock = stamp()
    c.execute("UPDATE entries SET status='EXPIRED' WHERE status='WAITING' AND start_at<=?", (clock,))
    for row in c.execute("SELECT * FROM entries WHERE status='OFFERED' AND offer_until<=?", (clock,)).fetchall():
        c.execute("UPDATE entries SET status='EXPIRED' WHERE id=?", (row["id"],))
        if row["end_at"]>clock:
            enqueue(c,row["lot_id"],row["offered_space_id"],row["start_at"],row["end_at"])


def create_app(settings=None):
    def handle(c, event):
        p = event["payload"]
        if event["type"] == "ParkingSlotReleased":
            enqueue(c,p["lot_id"],p["space_id"],p["start_at"],p["end_at"],event["id"])
        if event["type"] == "BookingCreated" and p.get("lot_id"):
            for row in c.execute("""SELECT * FROM entries WHERE user_id=? AND lot_id=? AND start_at=? AND end_at=?
                                 AND status IN ('WAITING','OFFERED')""",
                                 (p["user_id"],p["lot_id"],p["start_at"],p["end_at"])).fetchall():
                c.execute("UPDATE entries SET status='FULFILLED' WHERE id=?", (row["id"],))
                if row["offered_space_id"] and row["offered_space_id"]!=p["space_id"]:
                    enqueue(c,row["lot_id"],row["offered_space_id"],row["start_at"],row["end_at"])

    app = make_app("waitlist", SCHEMA, settings, handle_event=handle)
    db = app.state.db

    def availability(lot, start, end):
        return remote(app, "booking", "/bookings/availability?"+urlencode(
            {"lot_id":lot,"start_at":start,"end_at":end}))

    def process_one():
        with db.transaction() as c:
            expire(c)
            c.execute("DELETE FROM jobs WHERE end_at<=?", (stamp(),))
            job = c.execute("SELECT * FROM jobs ORDER BY created_at,id LIMIT 1").fetchone()
            if not job:
                return
            entries = c.execute("""SELECT * FROM entries WHERE status='WAITING' AND lot_id=?
               AND start_at<? AND end_at>? ORDER BY created_at,id""", (job["lot_id"],job["end_at"],job["start_at"])).fetchall()
        for entry in entries:
            try:
                result = availability(entry["lot_id"],entry["start_at"],entry["end_at"])
            except DomainError as exc:
                if exc.status in (404,422):
                    continue
                return
            with db.transaction() as c:
                expire(c)
                row = c.execute("SELECT * FROM entries WHERE id=? AND status='WAITING'", (entry["id"],)).fetchone()
                if not row:
                    continue
                for space in result["spaces"]:
                    if not space["available"] or (job["space_id"] and space["id"] != job["space_id"]):
                        continue
                    if c.execute("""SELECT 1 FROM entries WHERE status='OFFERED' AND offered_space_id=?
                         AND start_at<? AND end_at>?""",(space["id"],row["end_at"],row["start_at"])).fetchone():
                        continue
                    deadline = min(stamp(now()+timedelta(minutes=10)),row["start_at"])
                    c.execute("""UPDATE entries SET status='OFFERED',offered_space_id=?,space_code=?,offer_until=? WHERE id=?""",
                              (space["id"],space["code"],deadline,row["id"]))
                    emit(c,"WaitlistOfferCreated",{"entry_id":row["id"],"user_id":row["user_id"],"lot_name":row["lot_name"],
                        "space_code":space["code"],"offer_until":deadline})
                    c.execute("DELETE FROM jobs WHERE id=?", (job["id"],))
                    return
        with db.transaction() as c:
            c.execute("DELETE FROM jobs WHERE id=?", (job["id"],))

    app.state.periodic = process_one

    @app.get("/waitlist", tags=["Лист ожидания"])
    def mine(current: Actor):
        with db.transaction() as c:
            expire(c)
            return [dict(r) for r in c.execute("SELECT * FROM entries WHERE user_id=? ORDER BY created_at DESC LIMIT 200",
                                                (current["sub"],))]

    @app.post("/waitlist", status_code=201, tags=["Лист ожидания"])
    def join(body: WaitInput, current: Actor):
        start, end, _ = interval(body.start_at,body.end_at)
        key = (current["sub"],body.lot_id,start,end)
        sql = """SELECT * FROM entries WHERE user_id=? AND lot_id=? AND start_at=? AND end_at=?
                 AND status IN ('WAITING','OFFERED')"""
        with db.transaction() as c:
            expire(c)
            existing = c.execute(sql,key).fetchone()
            if existing:
                return dict(existing)
        result = availability(body.lot_id,start,end)
        if not result["lot"]["active"]:
            fail(409,"LOT_CLOSED","Парковка закрыта")
        if any(s["available"] for s in result["spaces"]):
            fail(409,"SPACES_AVAILABLE","Есть свободные места, обновите поиск")
        entry_id = uid()
        with db.transaction() as c:
            expire(c)
            existing = c.execute(sql,key).fetchone()
            if existing:
                return dict(existing)
            if c.execute("SELECT COUNT(*) FROM entries WHERE user_id=? AND status IN ('WAITING','OFFERED')",
                         (current["sub"],)).fetchone()[0]>=10:
                fail(409,"WAITLIST_LIMIT","Можно иметь до 10 активных заявок")
            c.execute("""INSERT INTO entries VALUES(?,?,?,?,?,?,'WAITING',NULL,NULL,NULL,?)""",
                      (entry_id,current["sub"],body.lot_id,result["lot"]["name"],start,end,stamp()))
            # Recheck after saving to cover a release arriving during signup.
            enqueue(c,body.lot_id,None,start,end)
            return dict(c.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone())

    @app.post("/waitlist/{entry_id}/cancel", tags=["Лист ожидания"])
    def cancel(entry_id: str, current: Actor):
        with db.transaction() as c:
            expire(c)
            row = c.execute("SELECT * FROM entries WHERE id=? AND user_id=?", (entry_id,current["sub"])).fetchone()
            if not row:
                fail(404,"WAITLIST_NOT_FOUND","Заявка не найдена")
            if row["status"] not in ("WAITING","OFFERED"):
                return dict(row)
            c.execute("UPDATE entries SET status='CANCELLED' WHERE id=?", (entry_id,))
            if row["offered_space_id"]:
                enqueue(c,row["lot_id"],row["offered_space_id"],row["start_at"],row["end_at"])
            return {**dict(row),"status":"CANCELLED"}

    return app


if __name__ != "__unit__":
    app = create_app()
