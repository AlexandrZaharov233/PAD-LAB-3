from datetime import datetime, timedelta
from urllib.parse import urlencode

from parking_common.runtime import Admin, DomainError, fail, make_app, remote, stamp

SCHEMA = """
CREATE TABLE IF NOT EXISTS report_bookings (
 id TEXT PRIMARY KEY, lot_id TEXT NOT NULL, lot_name TEXT NOT NULL, start_at TEXT NOT NULL,
 end_at TEXT NOT NULL, status TEXT NOT NULL, amount_cents INTEGER NOT NULL,
 discount_cents INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS report_payments (
 id TEXT PRIMARY KEY, booking_id TEXT NOT NULL, amount_cents INTEGER NOT NULL,
 status TEXT NOT NULL, created_at TEXT NOT NULL, refunded_at TEXT, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS bootstrap(source TEXT PRIMARY KEY, cursor TEXT NOT NULL, complete INTEGER NOT NULL);
INSERT OR IGNORE INTO bootstrap VALUES('booking','',0),('payment','',0);
CREATE INDEX IF NOT EXISTS report_bookings_created ON report_bookings(created_at);
CREATE INDEX IF NOT EXISTS report_payments_created ON report_payments(created_at);
CREATE INDEX IF NOT EXISTS report_payments_refunded ON report_payments(refunded_at);
"""


def save_booking(c, row):
    c.execute("""INSERT INTO report_bookings VALUES(?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET status=excluded.status,updated_at=excluded.updated_at
        WHERE excluded.updated_at>=report_bookings.updated_at""",
        tuple(row[k] for k in ("id","lot_id","lot_name","start_at","end_at","status","amount_cents",
                               "discount_cents","created_at","updated_at")))


def save_payment(c, row):
    c.execute("""INSERT INTO report_payments VALUES(?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET status=excluded.status,refunded_at=excluded.refunded_at,updated_at=excluded.updated_at
        WHERE excluded.updated_at>=report_payments.updated_at""",
        tuple(row[k] for k in ("id","booking_id","amount_cents","status","created_at","refunded_at","updated_at")))


def create_app(settings=None):
    def handle(c, event):
        data = event["payload"]
        if event["type"] in ("BookingCreated","BookingConfirmed","BookingCancelled","BookingExpired") and data.get("booking"):
            save_booking(c, data["booking"])
        if event["type"] in ("PaymentSucceeded","PaymentDeclined","PaymentRefunded") and data.get("payment"):
            save_payment(c, data["payment"])

    app = make_app("reporting", SCHEMA, settings, handle_event=handle)
    db = app.state.db

    def bootstrap():
        with db.read() as c:
            sources = c.execute("SELECT * FROM bootstrap WHERE complete=0 ORDER BY source").fetchall()
        for source in sources:
            name = source["source"]
            try:
                rows = remote(app, name, f"/internal/{name}s/reporting-snapshot?"+
                              urlencode({"after_id":source["cursor"]}), internal_call=True)
            except DomainError:
                continue
            with db.transaction() as c:
                for row in rows:
                    (save_booking if name == "booking" else save_payment)(c, row)
                c.execute("UPDATE bootstrap SET cursor=?,complete=? WHERE source=?",
                          (rows[-1]["id"] if rows else source["cursor"], int(len(rows)<100), name))

    app.state.periodic = bootstrap

    @app.get("/reports/summary", tags=["Отчёты"])
    def summary(start_at: datetime, end_at: datetime, current: Admin):
        if start_at.utcoffset() is None or end_at.utcoffset() is None:
            fail(422, "TIMEZONE_REQUIRED", "Укажите часовой пояс")
        if not timedelta(0) < end_at-start_at <= timedelta(days=366):
            fail(422, "INVALID_INTERVAL", "Выберите период от начала до окончания, максимум 366 дней")
        start, end = stamp(start_at), stamp(end_at)
        with db.read() as c:
            c.execute("BEGIN")
            states = {row["status"]:row["n"] for row in c.execute(
                "SELECT status,COUNT(*) AS n FROM report_bookings WHERE created_at>=? AND created_at<? GROUP BY status",
                (start,end))}
            totals = c.execute("""SELECT
              COALESCE(SUM(CASE WHEN status IN ('SUCCEEDED','REFUNDED') AND created_at>=? AND created_at<? THEN amount_cents END),0) AS paid,
              COALESCE(SUM(CASE WHEN status='REFUNDED' AND refunded_at>=? AND refunded_at<? THEN amount_cents END),0) AS refunded
              FROM report_payments""",(start,end,start,end)).fetchone()
            by_lot = [dict(row) for row in c.execute("""SELECT lot_id,MAX(lot_name) AS lot_name,COUNT(*) AS bookings,
                SUM(CASE WHEN status='CANCELLED' THEN 1 ELSE 0 END) AS cancelled
                FROM report_bookings WHERE created_at>=? AND created_at<? GROUP BY lot_id ORDER BY lot_name""",(start,end))]
            busy = c.execute("""SELECT lot_id,MAX(lot_name) AS lot_name,
                SUM((julianday(min(end_at,?))-julianday(max(start_at,?)))*24) AS hours
                FROM report_bookings WHERE status='CONFIRMED' AND start_at<? AND end_at>? GROUP BY lot_id""",
                (end,start,end,start)).fetchall()
            imported = not c.execute("SELECT 1 FROM bootstrap WHERE complete=0").fetchone()
            c.rollback()
        indexed = {r["lot_id"]:{**r,"reserved_hours":0} for r in by_lot}
        for row in busy:
            item = indexed.setdefault(row["lot_id"], {"lot_id":row["lot_id"],"lot_name":row["lot_name"],
                                                     "bookings":0,"cancelled":0})
            item["reserved_hours"] = round(row["hours"],2)
        return {"start_at":start,"end_at":end,"import_complete":imported,"currency":"MDL",
                "bookings":sum(states.values()),"statuses":states,"paid_cents":totals["paid"],
                "refunded_cents":totals["refunded"],"net_cents":totals["paid"]-totals["refunded"],
                "by_lot":sorted(indexed.values(),key=lambda row:row["lot_name"])}

    return app


if __name__ != "__unit__":
    app = create_app()
