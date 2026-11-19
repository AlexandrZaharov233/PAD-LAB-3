import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from parking_common.runtime import Settings, apply_event, now, stamp, uid
from test_services import booking_body, create_booking, payment


def post(s, service, path, body=None, who="admin"):
    return s["clients"][service].post(path,json=body,headers=s[who]["headers"])


def one_space(s):
    lot = post(s,"parking","/parking/lots",{"name":"Одна парковка","address":"Учебная улица, 9",
        "capacity":1,"hourly_rate_cents":2500}).json()
    space = s["clients"]["parking"].get("/parking/lots/"+lot["id"]+"/spaces").json()[0]
    body = booking_body(s,space_id=space["id"])
    return lot, body


def range_of(body, **extra):
    return {k:body[k] for k in ("start_at","end_at")} | extra


def report(s, who="admin"):
    return s["clients"]["reporting"].get("/reports/summary",headers=s[who]["headers"],
        params={"start_at":stamp(now()-timedelta(hours=1)),"end_at":stamp(now()+timedelta(days=2))})


def test_promo_discount_is_applied_on_server_and_charged(system):
    s=system
    body=booking_body(s,promo_code="park10")
    booking=create_booking(s,body)
    base=2*s["lot"]["hourly_rate_cents"]
    assert booking["base_amount_cents"]==base
    assert booking["amount_cents"]==base-base//10
    assert booking["promo_code"]=="PARK10"
    assert create_booking(s,body)["id"]==booking["id"]
    paid=payment(s,booking)
    assert paid.status_code==201 and paid.json()["amount_cents"]==booking["amount_cents"]
    s["drain"]()
    assert s["clients"]["booking"].get("/bookings/"+booking["id"],headers=s["user"]["headers"]).json()["status"]=="CONFIRMED"
    promos=s["clients"]["promotion"].get("/promotions",headers=s["admin"]["headers"]).json()
    assert next(p for p in promos if p["code"]=="PARK10")["used"]==1


def test_promo_capacity_is_reserved_and_released_on_unpaid_cancel(system):
    s=system
    assert post(s,"promotion","/promotions",{"code":"ONLYONE","percent":25,"max_uses":1}).status_code==201
    first=create_booking(s,booking_body(s,promo_code="ONLYONE"))
    second=booking_body(s,promo_code="ONLYONE",start_at=stamp(now()+timedelta(days=2)),end_at=stamp(now()+timedelta(days=2,hours=1)))
    rejected=post(s,"booking","/bookings",second,"user")
    assert rejected.status_code==409 and rejected.json()["error"]["code"]=="PROMO_LIMIT"
    assert post(s,"booking","/bookings/"+first["id"]+"/cancel",who="user").status_code==200
    s["drain"]()
    assert create_booking(s,second)["discount_cents"]>0


def test_promotion_conditions_and_access(system):
    s=system
    assert post(s,"promotion","/promotions",{"code":"NOADMIN","percent":10},"user").status_code==403
    lot,_=one_space(s)
    assert post(s,"promotion","/promotions",{"code":"LOT30","percent":30,"lot_id":lot["id"],"min_amount_cents":3000}).status_code==201
    quote={"code":"LOT30","lot_id":lot["id"],"base_amount_cents":2500}
    assert post(s,"promotion","/promotions/quote",quote,"user").json()["error"]["code"]=="PROMO_MINIMUM"
    quote["base_amount_cents"]=5000
    assert post(s,"promotion","/promotions/quote",{**quote,"lot_id":s["lot"]["id"]},"user").json()["error"]["code"]=="PROMO_LOT"
    assert post(s,"promotion","/promotions/quote",quote,"user").json()["amount_cents"]==3500
    assert s["clients"]["promotion"].patch("/promotions/LOT30",json={"active":False},headers=s["admin"]["headers"]).status_code==200
    assert post(s,"promotion","/promotions/quote",quote,"user").json()["error"]["code"]=="PROMO_INACTIVE"


def test_stale_promo_reservation_reconciles_with_booking(system):
    s=system
    b=create_booking(s,booking_body(s,promo_code="PARK10"))
    p=payment(s,b).json()
    # The booking confirms but its outgoing event has not yet reached Promotion.
    apply_event(s["apps"]["booking"],{"id":uid(),"type":"PaymentSucceeded","payload":{
        "payment_id":p["id"],"booking_id":b["id"],"user_id":b["user_id"],
        "amount_cents":b["amount_cents"],"currency":b["currency"]}})
    db=s["apps"]["promotion"].state.db
    with db.transaction() as c:
        c.execute("UPDATE claims SET expires_at=? WHERE booking_id=?",(stamp(now()-timedelta(seconds=1)),b["id"]))
    s["apps"]["promotion"].state.periodic()
    with db.read() as c:
        assert c.execute("SELECT state FROM claims WHERE booking_id=?",(b["id"],)).fetchone()[0]=="USED"


def test_maintenance_blocks_only_overlapping_intervals(system):
    s=system
    lot,body=one_space(s)
    block=range_of(body,space_id=body["space_id"],reason="Ремонт покрытия",idempotency_key=uid())
    response=post(s,"maintenance","/maintenance",block)
    assert response.status_code==201,response.text
    saved=response.json()
    assert saved["status"]=="ACTIVE"
    assert post(s,"maintenance","/maintenance",block).json()["id"]==saved["id"]
    lookup=s["clients"]["booking"].get("/bookings/availability",params=range_of(body,lot_id=lot["id"])).json()
    assert not lookup["spaces"][0]["available"] and lookup["spaces"][0]["maintenance"]
    assert post(s,"booking","/bookings",body,"user").json()["error"]["code"]=="SPACE_MAINTENANCE"
    other={**body,"start_at":body["end_at"],"end_at":stamp(now()+timedelta(days=1,hours=4)),"idempotency_key":uid()}
    assert post(s,"booking","/bookings",other,"user").status_code==201
    assert post(s,"maintenance","/maintenance/"+saved["id"]+"/cancel").json()["status"]=="CANCELLED"
    assert post(s,"booking","/bookings",body,"user").status_code==201


def test_maintenance_cannot_close_a_booked_slot_or_bypass_role(system):
    s=system
    b=create_booking(s)
    block=range_of(b,space_id=b["space_id"],reason="Проверка",idempotency_key=uid())
    assert post(s,"maintenance","/maintenance",block,"user").status_code==403
    response=post(s,"maintenance","/maintenance",block)
    assert response.status_code==409 and response.json()["error"]["code"]=="MAINTENANCE_BOOKED"
    assert s["clients"]["booking"].get("/bookings/"+b["id"],headers=s["user"]["headers"]).json()["status"]=="PENDING"
    assert s["clients"]["booking"].get("/internal/bookings/reporting-snapshot",headers=s["admin"]["headers"]).status_code==401


def test_concurrent_maintenance_and_booking_have_one_winner(system):
    s=system
    body=booking_body(s)
    block=range_of(body,space_id=body["space_id"],reason="Ремонт",idempotency_key=uid())
    with ThreadPoolExecutor(max_workers=2) as pool:
        p=pool.submit(post,s,"booking","/bookings",body,"user")
        m=pool.submit(post,s,"maintenance","/maintenance",block)
        results=[p.result(),m.result()]
    assert sorted(x.status_code for x in results)==[201,409],[(x.status_code,x.text) for x in results]


def test_maintenance_retries_after_booking_unavailable(system):
    s=system;body=booking_body(s);app=s["apps"]["maintenance"];original=app.state.http
    def broken(request):
        raise httpx.ConnectError("Down")
    app.state.http=httpx.Client(transport=httpx.MockTransport(broken))
    block=range_of(body,space_id=body["space_id"],reason="Проверка связи",idempotency_key=uid())
    try:
        assert post(s,"maintenance","/maintenance",block).status_code==503
    finally:
        app.state.http.close()
        app.state.http=original
    with app.state.db.transaction() as c:
        c.execute("UPDATE work_windows SET retry_at=?", (stamp(now()-timedelta(seconds=1)),))
    app.state.periodic()
    saved=s["clients"]["maintenance"].get("/maintenance",headers=s["admin"]["headers"]).json()
    assert len(saved)==1 and saved[0]["status"]=="ACTIVE"
    assert post(s,"maintenance","/maintenance",block).json()["id"]==saved[0]["id"]


def test_waitlist_fifo_notification_and_next_after_decline(system):
    s=system
    lot,body=one_space(s)
    queue=range_of(body,lot_id=lot["id"])
    assert post(s,"waitlist","/waitlist",queue,"other").json()["error"]["code"]=="SPACES_AVAILABLE"
    b=create_booking(s,body)
    first=post(s,"waitlist","/waitlist",queue,"other").json()
    assert first["status"]=="WAITING"
    assert post(s,"waitlist","/waitlist",queue,"other").json()["id"]==first["id"]
    s["third"]=s["register"]("third@example.test")
    second=post(s,"waitlist","/waitlist",queue,"third").json()
    assert post(s,"waitlist","/waitlist/"+first["id"]+"/cancel",who="third").status_code==404
    post(s,"booking","/bookings/"+b["id"]+"/cancel",who="user")
    s["drain"]()
    for _ in range(5):s["apps"]["waitlist"].state.periodic()
    s["drain"]()
    def mine(who):
        return s["clients"]["waitlist"].get("/waitlist",headers=s[who]["headers"]).json()[0]
    assert mine("other")["status"]=="OFFERED" and mine("third")["status"]=="WAITING"
    notes=s["clients"]["notification"].get("/notifications",headers=s["other"]["headers"]).json()
    assert sum(n["type"]=="WaitlistOfferCreated" for n in notes)==1
    assert post(s,"waitlist","/waitlist/"+first["id"]+"/cancel",who="other").status_code==200
    s["apps"]["waitlist"].state.periodic()
    assert mine("third")["id"]==second["id"] and mine("third")["status"]=="OFFERED"
    car=post(s,"profile","/profiles/me/vehicles",{"plate":"NEXT 123","label":"Очередь"},"third").json()
    assert post(s,"booking","/bookings",{**body,"vehicle_id":car["id"],"idempotency_key":uid()},"third").status_code==201
    s["drain"]()
    assert mine("third")["status"]=="FULFILLED"


def test_waitlist_rechecks_slot_before_making_offer(system):
    s=system
    lot,body=one_space(s)
    b=create_booking(s,body)
    assert post(s,"waitlist","/waitlist",range_of(body,lot_id=lot["id"]),"other").status_code==201
    post(s,"booking","/bookings/"+b["id"]+"/cancel",who="user")
    create_booking(s,{**body,"idempotency_key":uid()})
    s["drain"]()
    for _ in range(4):s["apps"]["waitlist"].state.periodic()
    assert s["clients"]["waitlist"].get("/waitlist",headers=s["other"]["headers"]).json()[0]["status"]=="WAITING"


def test_reporting_money_hours_and_refund(system):
    s=system
    assert report(s,"user").status_code==403
    b=create_booking(s)
    payment(s,b)
    s["drain"]()
    data=report(s).json()
    assert data["statuses"]=={"CONFIRMED":1}
    assert data["paid_cents"]==b["amount_cents"] and data["refunded_cents"]==0
    assert data["by_lot"][0]["reserved_hours"]==2
    post(s,"booking","/bookings/"+b["id"]+"/cancel",who="user")
    s["drain"]()
    data=report(s).json()
    assert data["statuses"]=={"CANCELLED":1}
    assert data["refunded_cents"]==b["amount_cents"] and data["net_cents"]==0
    assert data["by_lot"][0]["reserved_hours"]==0


def test_reporting_imports_saved_data_without_replaying_old_events(system):
    s=system
    b=create_booking(s)
    payment(s,b)
    # No events have been delivered to Reporting yet.
    assert report(s).json()["bookings"]==0
    s["apps"]["reporting"].state.periodic()
    data=report(s).json()
    assert data["import_complete"] and data["bookings"]==1 and data["paid_cents"]==b["amount_cents"]
    s["drain"]()
    data=report(s).json()
    assert data["bookings"]==1 and data["paid_cents"]==b["amount_cents"]


def test_v1_booking_database_upgrade_keeps_old_rows_and_retries(system,tmp_path):
    s=system
    path=tmp_path/"legacy-booking.db"
    data=booking_body(s)
    model=s["modules"]["booking"].BookingInput(**data)
    digest=hashlib.sha256(model.model_dump_json(exclude={"idempotency_key","promo_code"}).encode()).hexdigest()
    old_id=uid()
    amount=2*s["lot"]["hourly_rate_cents"]
    with sqlite3.connect(path) as c:
        c.execute("""CREATE TABLE bookings (
            id TEXT PRIMARY KEY,user_id TEXT NOT NULL,space_id TEXT NOT NULL,lot_id TEXT NOT NULL,
            lot_name TEXT NOT NULL,space_code TEXT NOT NULL,vehicle_id TEXT NOT NULL,plate TEXT NOT NULL,
            start_at TEXT NOT NULL,end_at TEXT NOT NULL,amount_cents INTEGER NOT NULL,currency TEXT NOT NULL,
            status TEXT NOT NULL,expires_at TEXT NOT NULL,payment_id TEXT,created_at TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,request_hash TEXT NOT NULL,UNIQUE(user_id,idempotency_key))""")
        c.execute("INSERT INTO bookings VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (old_id,s["user"]["user"]["id"],s["space"]["id"],s["lot"]["id"],s["lot"]["name"],s["space"]["code"],
             s["vehicle"]["id"],s["vehicle"]["plate"],data["start_at"],data["end_at"],amount,"MDL","PENDING",
             stamp(now()+timedelta(minutes=10)),None,stamp(),data["idempotency_key"],digest))
    cfg=Settings("booking",{**s["configuration"],"DATABASE_PATH":str(path)})
    for _ in range(2):
        app=s["modules"]["booking"].create_app(cfg)
        client=TestClient(app)
        retry=client.post("/bookings",json=data,headers=s["user"]["headers"])
        assert retry.status_code==201 and retry.json()["id"]==old_id
        row=retry.json()
        assert row["amount_cents"]==row["base_amount_cents"]==amount
        assert row["promo_code"] is None and row["discount_cents"]==0
        app.state.http.close()
        client.close()
