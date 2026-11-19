import httpx
from fastapi.testclient import TestClient

from conftest import load_module
from parking_common.runtime import Settings


def test_gateway_timeout_body_limit_and_request_id(configuration):
    app=load_module("gateway").create_app(Settings("gateway",configuration))
    with TestClient(app,raise_server_exceptions=False) as client:
        def timeout(request):raise httpx.ReadTimeout("upstream detail")
        import asyncio
        old=app.state.client
        app.state.client=httpx.AsyncClient(transport=httpx.MockTransport(timeout))
        response=client.get("/api/parking/lots",headers={"X-Request-ID":"gateway-test-trace"})
        assert response.status_code==504 and response.json()["error"]["request_id"]=="gateway-test-trace"
        assert client.post("/api/bookings",content=b"x"*65537).status_code==413
        assert client.get("/api/missing/path").status_code==404
        assert client.get("/").status_code==200
        assert "script-src 'self'" in client.get("/").headers["Content-Security-Policy"]
        asyncio.run(old.aclose())


def test_gateway_rate_limits_authentication(configuration):
    app=load_module("gateway").create_app(Settings("gateway",configuration))
    with TestClient(app,raise_server_exceptions=False) as client:
        old=app.state.client
        app.state.client=httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(401,json={"error":{"message":"denied"}})))
        for _ in range(30):assert client.post("/api/auth/login",json={}).status_code==401
        assert client.post("/api/auth/login",json={}).status_code==429
        import asyncio
        asyncio.run(old.aclose())
