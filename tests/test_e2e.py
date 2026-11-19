"""Black-box tests: HTTP to Gateway, real services and actual JetStream delivery."""
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import httpx
import pytest

pytestmark=[pytest.mark.e2e,pytest.mark.skipif(os.getenv("RUN_E2E")!="1",reason="Start the full application and set RUN_E2E=1")]
BASE=os.getenv("E2E_URL","http://127.0.0.1:8080")


def until(getter,predicate,seconds=18):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        value=getter()
        if predicate(value):return value
        time.sleep(.2)
    raise AssertionError(f"Expected eventual result within {seconds}s; last result: {value}")


@pytest.fixture
def live():
    client=httpx.Client(base_url=BASE,timeout=10,trust_env=False)
    email="test-"+uuid.uuid4().hex+"@example.test"
    response=client.post("/api/auth/register",json={"email":email,"password":"Integration-password-123","name":"Проверка API"})
    assert response.status_code==201,response.text
    client.headers["Authorization"]="Bearer "+response.json()["access_token"]
    admin=httpx.Client(base_url=BASE,timeout=10,trust_env=False)
    login=admin.post("/api/auth/login",json={"email":os.environ["E2E_ADMIN_EMAIL"],"password":os.environ["E2E_ADMIN_PASSWORD"]})
    assert login.status_code==200,login.text
    admin.headers["Authorization"]="Bearer "+login.json()["access_token"]
    vehicle=client.post("/api/profiles/me/vehicles",json={"plate":"TEST "+uuid.uuid4().hex[:5].upper(),"label":"Тестовый автомобиль"})
    assert vehicle.status_code==201,vehicle.text
    lot=admin.post("/api/parking/lots",json={"name":"Тестовая парковка "+uuid.uuid4().hex[:6],"address":"Учебный адрес, 20","hourly_rate_cents":2500,"capacity":3})
    assert lot.status_code==201,lot.text
    spaces=client.get("/api/parking/lots/"+lot.json()["id"]+"/spaces").json()
    start=datetime.now(timezone.utc)+timedelta(days=1);end=start+timedelta(hours=2)
    body={"space_id":spaces[0]["id"],"vehicle_id":vehicle.json()["id"],"start_at":start.isoformat(),"end_at":end.isoformat(),"idempotency_key":str(uuid.uuid4())}
    yield client,admin,lot.json(),spaces,body
    client.close();admin.close()


@pytest.mark.parametrize("service",["auth","profile","parking","booking","payment","notification",
                                    "promotion","waitlist","maintenance","reporting"])
def test_live_openapi_and_offline_docs(service):
    with httpx.Client(base_url=BASE,timeout=8,trust_env=False) as client:
        response=client.get("/api/openapi/"+service)
        assert response.status_code==200,response.text
        assert response.json()["servers"]==[{"url":"/api"}]
        assert client.get("/api/docs/"+service).status_code==200
        assert client.get("/swagger-assets/swagger-ui-bundle.js").status_code==200
        assert client.get("/api/health/"+service).status_code==200
        assert client.get("/api/ready/"+service).status_code==200


def test_live_booking_payment_notification_and_refund(live):
    client,admin,lot,spaces,body=live
    availability=client.get("/api/bookings/availability",params={"lot_id":lot["id"],"start_at":body["start_at"],"end_at":body["end_at"]})
    assert availability.status_code==200 and availability.json()["spaces"][0]["available"]
    response=client.post("/api/bookings",json=body,headers={"X-Request-ID":"e2e-request-through-services"})
    assert response.status_code==201,response.text
    assert response.headers["X-Request-ID"]=="e2e-request-through-services"
    booking=response.json();assert booking["amount_cents"]==5000
    assert client.post("/api/bookings",json=body).json()["id"]==booking["id"]
    declined=client.post("/api/payments",json={"booking_id":booking["id"],"outcome":"decline","idempotency_key":str(uuid.uuid4())})
    assert declined.status_code==201 and declined.json()["status"]=="DECLINED"
    payment_body={"booking_id":booking["id"],"idempotency_key":str(uuid.uuid4())}
    paid=client.post("/api/payments",json=payment_body);assert paid.status_code==201,paid.text
    assert client.post("/api/payments",json=payment_body).json()["id"]==paid.json()["id"]
    until(lambda:client.get("/api/bookings/"+booking["id"]).json(),lambda b:b["status"]=="CONFIRMED")
    notices=until(lambda:client.get("/api/notifications").json(),lambda rows:any(r["type"]=="BookingConfirmed" for r in rows))
    note=next(n for n in notices if n["type"]=="BookingConfirmed")
    assert client.post("/api/notifications/"+note["id"]+"/read").status_code==200
    assert client.post("/api/bookings/"+booking["id"]+"/cancel").status_code==200
    until(lambda:client.get("/api/payments").json(),lambda rows:any(p["id"]==paid.json()["id"] and p["status"]=="REFUNDED" for p in rows))
    forbidden=client.patch("/api/parking/lots/"+lot["id"],json={"hourly_rate_cents":100,"active":True})
    assert forbidden.status_code==403
    assert client.get("/api/status").json()["ready"]


def test_live_simultaneous_booking_one_winner(live):
    client,admin,lot,spaces,body=live
    bodies=[{**body,"idempotency_key":str(uuid.uuid4())} for _ in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses=list(pool.map(lambda b:client.post("/api/bookings",json=b),bodies))
    assert sorted(r.status_code for r in responses)==[201,409],[(r.status_code,r.text) for r in responses]
    winner=next(r.json() for r in responses if r.status_code==201)
    assert client.post("/api/bookings/"+winner["id"]+"/cancel").status_code==200


def test_live_unauthenticated_and_bad_input():
    with httpx.Client(base_url=BASE,timeout=8,trust_env=False) as client:
        assert client.get("/api/bookings").status_code==401
        response=client.post("/api/auth/register",json={"email":"wrong","password":"short","name":"X"})
        assert response.status_code==422 and "request_id" in response.json()["error"]
        assert "short" not in response.text
        assert client.get("/api/unknown/resource").status_code==404


def test_live_expansion_waitlist_discount_maintenance_and_reporting(live):
    client,admin,lot,spaces,body=live
    began=datetime.now(timezone.utc)
    window={k:body[k] for k in ("start_at","end_at")}
    for space in spaces[1:]:
        blocked=admin.post("/api/maintenance",json={**window,"space_id":space["id"],
            "reason":"Проверка обслуживания","idempotency_key":str(uuid.uuid4())})
        assert blocked.status_code==201,blocked.text
    occupied=client.post("/api/bookings",json=body)
    assert occupied.status_code==201,occupied.text
    available=client.get("/api/bookings/availability",params={**window,"lot_id":lot["id"]}).json()["spaces"]
    assert not any(x["available"] for x in available) and sum(x["maintenance"] for x in available)==2
    with httpx.Client(base_url=BASE,timeout=10,trust_env=False) as next_user:
        register=next_user.post("/api/auth/register",json={"email":"queue-"+uuid.uuid4().hex+"@example.test",
            "name":"Ожидание","password":"Integration-password-123"})
        assert register.status_code==201,register.text
        next_user.headers["Authorization"]="Bearer "+register.json()["access_token"]
        vehicle=next_user.post("/api/profiles/me/vehicles",json={"plate":"QUEUE 123","label":"Тест"}).json()
        joined=next_user.post("/api/waitlist",json={**window,"lot_id":lot["id"]})
        assert joined.status_code==201 and joined.json()["status"]=="WAITING",joined.text
        assert client.post("/api/bookings/"+occupied.json()["id"]+"/cancel").status_code==200
        offer=until(lambda:next_user.get("/api/waitlist").json()[0],lambda w:w["status"]=="OFFERED")
        assert offer["offered_space_id"]==spaces[0]["id"]
        until(lambda:next_user.get("/api/notifications").json(),lambda rows:any(n["type"]=="WaitlistOfferCreated" for n in rows))
        quoted=next_user.post("/api/promotions/quote",json={"code":"PARK10","lot_id":lot["id"],"base_amount_cents":5000})
        assert quoted.json()["amount_cents"]==4500
        booked=next_user.post("/api/bookings",json={**body,"vehicle_id":vehicle["id"],"promo_code":"PARK10",
                                                  "idempotency_key":str(uuid.uuid4())})
        assert booked.status_code==201 and booked.json()["amount_cents"]==4500,booked.text
        bid=booked.json()["id"]
        assert next_user.post("/api/payments",json={"booking_id":bid,"idempotency_key":str(uuid.uuid4())}).status_code==201
        until(lambda:next_user.get("/api/bookings/"+bid).json(),lambda b:b["status"]=="CONFIRMED")
        until(lambda:next_user.get("/api/waitlist").json()[0],lambda w:w["status"]=="FULFILLED")
        report_params={"start_at":began.isoformat(),"end_at":(began+timedelta(days=2)).isoformat()}
        report=until(lambda:admin.get("/api/reports/summary",params=report_params).json(),
            lambda r:r["import_complete"] and r["paid_cents"]==4500 and any(
                row["lot_id"]==lot["id"] and row["reserved_hours"]==2 for row in r["by_lot"]))
        assert report["statuses"]=={"CANCELLED":1,"CONFIRMED":1}
        assert next_user.post("/api/bookings/"+bid+"/cancel").status_code==200
        until(lambda:admin.get("/api/reports/summary",params=report_params).json(),
              lambda r:r["refunded_cents"]==4500 and r["net_cents"]==0)
    assert len(client.get("/api/status").json()["services"])==10
    assert admin.get("/api/bookings/internal/bookings/reporting-snapshot").status_code==404
