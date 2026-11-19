import asyncio
import os
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from parking_common.runtime import (Settings, fail, install_errors_and_logs, install_web,
                                    logger, request_id)

ROUTES = {"auth": "auth", "profiles": "profile", "parking": "parking",
          "bookings": "booking", "payments": "payment", "notifications": "notification",
          "promotions": "promotion", "waitlist": "waitlist", "maintenance": "maintenance", "reports": "reporting"}
WEB = Path(__file__).parent / "web"


def create_app(settings=None):
    cfg = settings or Settings("gateway")
    log = logger("gateway")
    pod_name = os.getenv("PARKSLOT_POD_NAME")

    @asynccontextmanager
    async def lifespan(app):
        app.state.client = httpx.AsyncClient(timeout=cfg.timeout+1, trust_env=False)
        yield
        await app.state.client.aclose()

    app = FastAPI(title="ParkSlot API Gateway", version="1.1.0", docs_url=None, redoc_url=None, lifespan=lifespan)
    install_errors_and_logs(app, log)
    install_web(app, "gateway")
    app.state.limits = defaultdict(deque)

    @app.middleware("http")
    async def browser_headers(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "same-origin"
        if pod_name:
            response.headers["X-ParkSlot-Instance"] = pod_name
        if request.url.path == "/" or request.url.path.startswith("/assets"):
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'")
        return response

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(WEB / "index.html")

    app.mount("/assets", StaticFiles(directory=WEB), name="assets")

    @app.get("/health", tags=["Health"])
    def health():
        return {"status": "ok", "service": "gateway"}

    async def status_one(name):
        try:
            response = await app.state.client.get(cfg.urls[name]+"/ready", headers={"X-Request-ID": request_id.get()}, timeout=2)
            return {"service": name, "ready": response.status_code == 200}
        except httpx.RequestError:
            return {"service": name, "ready": False}

    @app.get("/api/status", tags=["Health"])
    async def status():
        rows = await asyncio.gather(*(status_one(name) for name in cfg.urls))
        return {"ready": all(x["ready"] for x in rows), "services": rows}

    @app.get("/ready", tags=["Health"])
    async def ready():
        result = await status()
        return JSONResponse(result, status_code=200 if result["ready"] else 503)

    @app.get("/api/openapi/{service}", include_in_schema=False)
    async def spec(service: str):
        if service not in cfg.urls:
            fail(404, "SERVICE_NOT_FOUND", "Сервис не найден")
        try:
            response = await app.state.client.get(cfg.urls[service]+"/openapi.json")
            response.raise_for_status()
            result = response.json()
        except (httpx.HTTPError, ValueError):
            fail(503, "UPSTREAM_UNAVAILABLE", "Документация сервиса временно недоступна")
        result["servers"] = [{"url": "/api"}]
        for probe in ("health", "ready"):
            operation = result["paths"].pop("/"+probe, None)
            if operation:
                result["paths"]["/"+probe+"/"+service] = operation
        return result

    @app.get("/api/health/{service}", include_in_schema=False)
    @app.get("/api/ready/{service}", include_in_schema=False)
    async def service_probe(service: str, request: Request):
        if service not in cfg.urls:
            fail(404, "SERVICE_NOT_FOUND", "Сервис не найден")
        probe = request.url.path.split("/")[2]
        try:
            response = await app.state.client.get(cfg.urls[service]+"/"+probe)
        except httpx.RequestError:
            fail(503, "UPSTREAM_UNAVAILABLE", "Сервис временно недоступен")
        return Response(response.content, status_code=response.status_code, media_type="application/json")

    @app.get("/api/docs/{service}", include_in_schema=False)
    def service_docs(service: str):
        if service not in cfg.urls:
            fail(404, "SERVICE_NOT_FOUND", "Сервис не найден")
        return get_swagger_ui_html(openapi_url=f"/api/openapi/{service}", title=service+" API",
            swagger_js_url="/swagger-assets/swagger-ui-bundle.js",
            swagger_css_url="/swagger-assets/swagger-ui.css", swagger_favicon_url="")

    @app.api_route("/api/{resource}", methods=["GET", "POST", "PATCH", "DELETE"], include_in_schema=False)
    @app.api_route("/api/{resource}/{path:path}", methods=["GET", "POST", "PATCH", "DELETE"], include_in_schema=False)
    async def proxy(resource: str, request: Request, path: str = ""):
        if resource not in ROUTES or any(part in ("..", ".", "internal") for part in path.split("/")):
            fail(404, "ROUTE_NOT_FOUND", "Маршрут не найден")
        if resource == "auth" and request.method == "POST":
            key = request.client.host if request.client else "local"
            hits = app.state.limits[key]
            clock = time.monotonic()
            while hits and hits[0] < clock-60:
                hits.popleft()
            if len(hits) >= 30:
                fail(429, "RATE_LIMITED", "Слишком много запросов входа. Повторите через минуту")
            hits.append(clock)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 65536:
                fail(413, "BODY_TOO_LARGE", "Слишком большой запрос")
        service = ROUTES[resource]
        target = cfg.urls[service] + "/" + resource + ("/"+path if path else "")
        headers = {"X-Request-ID": request_id.get()}
        for header in ("Authorization", "Content-Type"):
            if header in request.headers:
                headers[header] = request.headers[header]
        try:
            response = await app.state.client.request(request.method, target,
                params=request.query_params.multi_items(), content=bytes(body), headers=headers)
        except httpx.TimeoutException:
            fail(504, "UPSTREAM_TIMEOUT", "Сервис не ответил вовремя. Повторите запрос")
        except httpx.RequestError:
            fail(503, "UPSTREAM_UNAVAILABLE", "Сервис временно недоступен")
        return Response(response.content, status_code=response.status_code,
                        media_type=response.headers.get("Content-Type", "application/json"))

    return app


if __name__ != "__unit__":
    app = create_app()
