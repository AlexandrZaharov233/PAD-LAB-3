import base64
import json
import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta

import httpx
import jwt
import pytest

from parking_common.runtime import (DomainError, JsonLog, Settings, apply_event, interval,
                                    now, stamp, uid)


def booking_body(s, **changes):
    start=now()+timedelta(days=1)
    return {"space_id":s["space"]["id"],"vehicle_id":s["vehicle"]["id"],
            "start_at":stamp(start),"end_at":stamp(start+timedelta(hours=2)),
            "idempotency_key":uid(),**changes}


def create_booking(s, body=None):
    response=s["clients"]["booking"].post("/bookings",json=body or booking_body(s),headers=s["user"]["headers"])
    assert response.status_code==201,response.text
    return response.json()


def payment(s, booking, **changes):
    return s["clients"]["payment"].post("/payments",headers=s["user"]["headers"],
            json={"booking_id":booking["id"],"idempotency_key":uid(),**changes})


def test_registration_password_hash_and_no_privilege_escalation(system):
    s=system
    response=s["clients"]["auth"].post("/auth/register",json={"email":"bad@example.test","password":"LongPassword123","name":"Test","role":"admin"})
    assert response.status_code==422
    response=s["clients"]["auth"].post("/auth/register",json={"email":"USER@example.test","password":"LongPassword123","name":"Test"})
    assert response.status_code==409
    with s["apps"]["auth"].state.db.read() as c:
        row=c.execute("SELECT * FROM accounts WHERE email='user@example.test'").fetchone()
    assert row["role"]=="user" and "Password-for-tests-123" not in row["password_hash"]
    assert "password_hash" not in s["user"]["user"]


def test_login_failures_are_committed_and_rate_limited(system):
    client=system["clients"]["auth"]
    for _ in range(8):
        response=client.post("/auth/login",json={"email":"unknown@example.test","password":"InvalidPassword123"})
        assert response.status_code==401
    response=client.post("/auth/login",json={"email":"unknown@example.test","password":"InvalidPassword123"})
    assert response.status_code==429
    assert "traceback" not in response.text.lower()


@pytest.mark.parametrize("service,path",[("profile","/profiles/me"),("booking","/bookings"),("payment","/payments"),("notification","/notifications")])
def test_services_require_authentication(system,service,path):
    assert system["clients"][service].get(path).status_code==401
    assert system["clients"][service].get(path,headers={"Authorization":"Bearer forged.jwt.token"}).status_code==401


def test_expired_signed_token_is_rejected(system):
    key=base64.b64decode(system["configuration"]["JWT_PRIVATE_KEY"])
    token=jwt.encode({"sub":"test","email":"a@b.test","role":"user","iat":now()-timedelta(hours=3),
                      "exp":now()-timedelta(hours=1),"iss":"parking-auth","aud":"parking-api"},key,algorithm="EdDSA")
    assert system["clients"]["profile"].get("/profiles/me",headers={"Authorization":"Bearer "+token}).status_code==401


def test_admin_only_parking_changes_and_persistence(system):
    s=system;path="/parking/lots/"+s["lot"]["id"];body={"hourly_rate_cents":9900,"active":True}
    assert s["clients"]["parking"].patch(path,json=body,headers=s["user"]["headers"]).status_code==403
    assert s["clients"]["parking"].patch(path,json=body,headers=s["admin"]["headers"]).status_code==200
    new=s["modules"]["parking"].create_app(s["apps"]["parking"].state.config)
    with new.state.db.read() as c:assert c.execute("SELECT hourly_rate_cents FROM lots WHERE id=?",(s["lot"]["id"],)).fetchone()[0]==9900
    new.state.http.close()


def test_vehicle_normalization_and_owner_access(system):
    s=system;c=s["clients"]["profile"]
    response=c.post("/profiles/me/vehicles",headers=s["user"]["headers"],json={"plate":"abc   123","label":"Another"})
    assert response.status_code==409
    path="/profiles/me/vehicles/"+s["vehicle"]["id"]
    assert c.get(path,headers=s["other"]["headers"]).status_code==404
    assert c.delete(path,headers=s["other"]["headers"]).status_code==404


def test_price_is_server_calculated_and_frozen(system):
    s=system;b=create_booking(s)
    # The interval helper uses one shared start for deterministic two-hour charging.
    assert b["amount_cents"]==2*s["lot"]["hourly_rate_cents"]
    changed=s["clients"]["parking"].patch("/parking/lots/"+s["lot"]["id"],json={"hourly_rate_cents":10000,"active":True},headers=s["admin"]["headers"])
    assert changed.status_code==200
    assert s["clients"]["booking"].get("/bookings/"+b["id"],headers=s["user"]["headers"]).json()["amount_cents"]==b["amount_cents"]
    response=s["clients"]["booking"].post("/bookings",json={**booking_body(s),"amount_cents":1},headers=s["user"]["headers"])
    assert response.status_code==422


def test_overlap_rejected_adjacent_allowed(system):
    s=system;start=now()+timedelta(days=1);end=start+timedelta(hours=2)
    first=create_booking(s,booking_body(s,start_at=stamp(start),end_at=stamp(end)))
    response=s["clients"]["booking"].post("/bookings",headers=s["user"]["headers"],json=booking_body(s,start_at=stamp(start+timedelta(minutes=30)),end_at=stamp(end+timedelta(hours=1))))
    assert response.status_code==409 and response.json()["error"]["code"]=="SPACE_OCCUPIED"
    create_booking(s,booking_body(s,start_at=stamp(end),end_at=stamp(end+timedelta(hours=1))))


def test_concurrent_overlap_only_one_wins(system):
    s=system;start=now()+timedelta(days=2);end=start+timedelta(hours=1)
    bodies=[booking_body(s,start_at=stamp(start),end_at=stamp(end)) for _ in range(2)]
    def send(body):return s["clients"]["booking"].post("/bookings",json=body,headers=s["user"]["headers"]).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:result=list(pool.map(send,bodies))
    assert sorted(result)==[201,409]


def test_booking_idempotency_and_conflicting_payload(system):
    s=system;body=booking_body(s);first=create_booking(s,body);second=create_booking(s,body)
    assert first["id"]==second["id"]
    response=s["clients"]["booking"].post("/bookings",json={**body,"space_id":"other"},headers=s["user"]["headers"])
    assert response.status_code==409 and response.json()["error"]["code"]=="IDEMPOTENCY_CONFLICT"


def test_another_user_cannot_read_or_cancel_booking(system):
    s=system;b=create_booking(s);path="/bookings/"+b["id"]
    assert s["clients"]["booking"].get(path,headers=s["other"]["headers"]).status_code==404
    assert s["clients"]["booking"].post(path+"/cancel",headers=s["other"]["headers"]).status_code==404
    assert s["clients"]["payment"].post("/payments",json={"booking_id":b["id"],"idempotency_key":uid()},headers=s["other"]["headers"]).status_code==404


def test_disabled_space_cannot_be_booked(system):
    s=system;s["clients"]["parking"].patch("/parking/spaces/"+s["space"]["id"],json={"active":False},headers=s["admin"]["headers"])
    response=s["clients"]["booking"].post("/bookings",json=booking_body(s),headers=s["user"]["headers"])
    assert response.status_code==409 and response.json()["error"]["code"]=="SPACE_DISABLED"


def test_expiration_releases_space_and_prevents_payment(system):
    s=system;b=create_booking(s)
    with s["apps"]["booking"].state.db.transaction() as c:
        c.execute("UPDATE bookings SET expires_at=? WHERE id=?",(stamp(now()-timedelta(minutes=1)),b["id"]))
    assert s["clients"]["booking"].get("/bookings/"+b["id"],headers=s["user"]["headers"]).json()["status"]=="EXPIRED"
    assert payment(s,b).status_code==409
    create_booking(s)


def test_decline_then_success_and_eventual_confirmation(system):
    s=system;b=create_booking(s)
    assert payment(s,b,outcome="decline").json()["status"]=="DECLINED"
    result=payment(s,b);assert result.status_code==201
    s["drain"]()
    booking=s["clients"]["booking"].get("/bookings/"+b["id"],headers=s["user"]["headers"]).json()
    assert booking["status"]=="CONFIRMED" and booking["payment_id"]==result.json()["id"]


def test_payment_retry_does_not_create_duplicate(system):
    s=system;b=create_booking(s);key=uid()
    first=payment(s,b,idempotency_key=key);second=payment(s,b,idempotency_key=key)
    assert first.status_code==201 and second.json()["id"]==first.json()["id"]
    assert payment(s,b).status_code==409


def test_cancel_after_confirmation_refunds_payment(system):
    s=system;b=create_booking(s);assert payment(s,b).status_code==201;s["drain"]()
    assert s["clients"]["booking"].post("/bookings/"+b["id"]+"/cancel",headers=s["user"]["headers"]).status_code==200
    s["drain"]()
    assert s["clients"]["payment"].get("/payments",headers=s["user"]["headers"]).json()[0]["status"]=="REFUNDED"


def test_cancel_before_payment_event_is_compensated(system):
    s=system;b=create_booking(s);result=payment(s,b);assert result.status_code==201
    s["clients"]["booking"].post("/bookings/"+b["id"]+"/cancel",headers=s["user"]["headers"])
    s["drain"]()
    assert s["clients"]["booking"].get("/bookings/"+b["id"],headers=s["user"]["headers"]).json()["status"]=="CANCELLED"
    assert s["clients"]["payment"].get("/payments",headers=s["user"]["headers"]).json()[0]["status"]=="REFUNDED"


def test_duplicate_events_do_not_duplicate_notifications(system):
    s=system;event={"id":uid(),"type":"BookingCreated","payload":{"user_id":s["user"]["user"]["id"],"booking_id":uid()},"request_id":"test-event-id"}
    app=s["apps"]["notification"]
    assert apply_event(app,event) is True
    assert apply_event(app,event) is False
    data=s["clients"]["notification"].get("/notifications",headers=s["user"]["headers"]).json()
    assert len(data)==1
    assert s["clients"]["notification"].post("/notifications/"+data[0]["id"]+"/read",headers=s["other"]["headers"]).status_code==404


def test_disabled_notifications_are_respected(system):
    s=system
    s["clients"]["profile"].patch("/profiles/me",json={"name":"Пользователь","notifications_enabled":False},headers=s["user"]["headers"])
    s["drain"]();create_booking(s);s["drain"]()
    assert s["clients"]["notification"].get("/notifications",headers=s["user"]["headers"]).json()==[]


def test_event_failure_rolls_back_receipt(system):
    event={"id":uid(),"type":"BookingCreated","payload":{},"request_id":"test-failed-event"}
    with pytest.raises(KeyError):apply_event(system["apps"]["notification"],event)
    with system["apps"]["notification"].state.db.read() as c:
        assert not c.execute("SELECT 1 FROM inbox WHERE id=?",(event["id"],)).fetchone()


@pytest.mark.parametrize("error,status",[(httpx.ConnectError("down"),503),(httpx.ReadTimeout("late"),504)])
def test_dependency_failure_has_safe_error(system,error,status):
    s=system
    def broken(request):raise error
    s["apps"]["booking"].state.http.close();s["apps"]["booking"].state.http=httpx.Client(transport=httpx.MockTransport(broken))
    response=s["clients"]["booking"].post("/bookings",headers={**s["user"]["headers"],"X-Request-ID":"test-trace-1234"},json=booking_body(s))
    assert response.status_code==status
    assert response.json()["error"]["request_id"]=="test-trace-1234"
    assert "Traceback" not in response.text


def test_database_failure_is_not_exposed(system):
    @contextmanager
    def broken():raise sqlite3.OperationalError("secret_database_filename");yield
    system["apps"]["parking"].state.db.read=broken
    response=system["clients"]["parking"].get("/parking/lots")
    assert response.status_code==503 and "secret_database_filename" not in response.text


def test_health_and_readiness_differ(system):
    app=system["apps"]["profile"];client=system["clients"]["profile"]
    app.state.bus_ready=False
    assert client.get("/health").status_code==200 and client.get("/ready").status_code==503


def test_request_id_reaches_downstream_service(system):
    s=system;client=s["apps"]["booking"].state.http;original=client._transport.handler;seen=[]
    def capture(request):seen.append(request.headers.get("x-request-id"));return original(request)
    client._transport=httpx.MockTransport(capture)
    response=s["clients"]["booking"].post("/bookings",json=booking_body(s),headers={**s["user"]["headers"],"X-Request-ID":"trace-through-services"})
    assert response.status_code==201
    assert seen==["trace-through-services","trace-through-services"]
    assert response.headers["X-Request-ID"]=="trace-through-services"


@pytest.mark.parametrize("name",["auth","profile","parking","booking","payment","notification"])
def test_openapi_and_offline_swagger_assets(system,name):
    client=system["clients"][name]
    response=client.get("/openapi.json")
    assert response.status_code==200 and response.json()["paths"]
    assert "/swagger-assets/swagger-ui-bundle.js" in client.get("/docs").text
    assert client.get("/swagger-assets/swagger-ui.css").status_code==200


@pytest.mark.parametrize("minutes",[0,10,1441])
def test_invalid_duration_rejected(minutes):
    start=now()+timedelta(days=1)
    with pytest.raises(DomainError):interval(start,start+timedelta(minutes=minutes))


def test_naive_time_rejected():
    start=(now()+timedelta(days=1)).replace(tzinfo=None)
    with pytest.raises(DomainError):interval(start,start+timedelta(hours=1))
