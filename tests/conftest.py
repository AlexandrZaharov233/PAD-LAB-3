import base64
import importlib.util
import json
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from parking_common.runtime import SERVICE_PORTS, Settings, apply_event, stamp, uid

ROOT = Path(__file__).resolve().parents[1]


def load_module(service):
    path = ROOT/"gateway"/"app.py" if service=="gateway" else ROOT/"services"/(service+"-service")/"app.py"
    spec = importlib.util.spec_from_file_location("__unit__",path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def configuration(tmp_path):
    key=Ed25519PrivateKey.generate()
    return {
        "JWT_PRIVATE_KEY":base64.b64encode(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption())).decode(),
        "JWT_PUBLIC_KEY":base64.b64encode(key.public_key().public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo)).decode(),
        "INTERNAL_API_KEY":"test-internal-key-that-is-long-enough", "TESTING":"1",
        "ADMIN_EMAIL":"admin@example.test", "ADMIN_PASSWORD":"Test-admin-password-123",
        **{n.upper()+"_URL":"http://"+n for n in SERVICE_PORTS},
    }


@pytest.fixture
def system(tmp_path, configuration):
    apps={};clients={};modules={}
    for name in SERVICE_PORTS:
        modules[name]=load_module(name)
        cfg=Settings(name,{**configuration,"DATABASE_PATH":str(tmp_path/(name+".db"))})
        apps[name]=modules[name].create_app(cfg)
        clients[name]=TestClient(apps[name],raise_server_exceptions=False)

    def dispatch(request):
        target=clients[request.url.host]
        response=target.request(request.method,str(request.url),content=request.content,headers=dict(request.headers))
        return httpx.Response(response.status_code,content=response.content,headers=response.headers)

    for app in apps.values():
        app.state.http.close()
        app.state.http=httpx.Client(transport=httpx.MockTransport(dispatch),timeout=2)

    def drain():
        count=0
        for _ in range(20):
            progress=0
            for producer,app in apps.items():
                with app.state.db.read() as c:
                    rows=c.execute("SELECT * FROM outbox WHERE published_at IS NULL ORDER BY seq").fetchall()
                for row in rows:
                    event={"id":row["id"],"type":row["type"],"payload":json.loads(row["payload"]),
                           "request_id":row["request_id"],"created_at":row["created_at"],"producer":producer,"version":1}
                    for receiver in apps.values():apply_event(receiver,event)
                    with app.state.db.transaction() as c:c.execute("UPDATE outbox SET published_at=? WHERE id=?",(stamp(),row["id"]))
                    progress+=1;count+=1
            if not progress:return count
        raise AssertionError("Event cycle did not converge")

    def register(email="user@example.test",name="Пользователь"):
        response=clients["auth"].post("/auth/register",json={"email":email,"password":"Password-for-tests-123","name":name})
        assert response.status_code==201,response.text
        result=response.json();result["headers"]={"Authorization":"Bearer "+result["access_token"]}
        return result

    user=register()
    other=register("other@example.test","Другой пользователь")
    admin=clients["auth"].post("/auth/login",json={"email":configuration["ADMIN_EMAIL"],"password":configuration["ADMIN_PASSWORD"]}).json()
    admin["headers"]={"Authorization":"Bearer "+admin["access_token"]}
    vehicle=clients["profile"].post("/profiles/me/vehicles",headers=user["headers"],json={"plate":"ABC 123","label":"Мой автомобиль"}).json()
    lot=clients["parking"].get("/parking/lots").json()[0]
    space=clients["parking"].get("/parking/lots/"+lot["id"]+"/spaces").json()[0]
    yield {"apps":apps,"clients":clients,"modules":modules,"drain":drain,"register":register,
           "user":user,"other":other,"admin":admin,"vehicle":vehicle,"lot":lot,"space":space,"configuration":configuration}
    for client in clients.values():client.close()
    for app in apps.values():app.state.http.close()
