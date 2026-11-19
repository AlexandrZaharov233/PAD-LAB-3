from __future__ import annotations

import base64
import contextvars
import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

import httpx
import jwt
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from pydantic import BaseModel, ConfigDict

request_id = contextvars.ContextVar("request_id", default="startup")
SERVICE_PORTS = {"auth": 8011, "profile": 8012, "parking": 8013,
                 "booking": 8014, "payment": 8015, "notification": 8016,
                 "promotion": 8017, "waitlist": 8018, "maintenance": 8019, "reporting": 8020}


def now() -> datetime:
    return datetime.now(timezone.utc)


def stamp(value: datetime | None = None) -> str:
    return (value or now()).astimezone(timezone.utc).isoformat(timespec="microseconds")


def uid() -> str:
    return str(uuid.uuid4())


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class DomainError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message


def fail(status: int, code: str, message: str):
    raise DomainError(status, code, message)


class JsonLog(logging.Formatter):
    def format(self, record):
        item = {"timestamp": stamp(), "service": getattr(record, "service", "runtime"),
                "level": record.levelname, "request_id": request_id.get(),
                "message": record.getMessage()}
        for name in ("event_id", "method", "path", "status", "elapsed_ms"):
            if hasattr(record, name):
                item[name] = getattr(record, name)
        if record.exc_info:
            item["exception"] = self.formatException(record.exc_info)
        return json.dumps(item, ensure_ascii=False)


class ServiceLog(logging.LoggerAdapter):
    def process(self, msg, kwargs):
        kwargs["extra"] = {**self.extra, **kwargs.get("extra", {})}
        return msg, kwargs


def logger(service):
    log = logging.getLogger("parking." + service)
    if not log.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(JsonLog())
        log.addHandler(handler)
        log.setLevel(os.getenv("LOG_LEVEL", "INFO"))
        log.propagate = False
    return ServiceLog(log, {"service": service})


class Settings:
    def __init__(self, service: str, overrides: dict | None = None):
        values = {**os.environ, **(overrides or {})}
        self.service = service
        self.db = values.get("DATABASE_PATH", f"data/{service}.db")
        self.public_key = values.get("JWT_PUBLIC_KEY", "")
        self.private_key = values.get("JWT_PRIVATE_KEY", "")
        self.internal_key = values.get("INTERNAL_API_KEY", "")
        self.nats_url = values.get("NATS_URL", "nats://127.0.0.1:4222")
        self.nats_user = values.get("NATS_USER", "parking")
        self.nats_password = values.get("NATS_PASSWORD", "")
        self.testing = values.get("TESTING") == "1"
        self.admin_email = values.get("ADMIN_EMAIL", "admin@parking.local")
        self.admin_password = values.get("ADMIN_PASSWORD", "")
        self.urls = {name: values.get(name.upper() + "_URL", f"http://127.0.0.1:{port}")
                     for name, port in SERVICE_PORTS.items()}
        self.timeout = float(values.get("HTTP_TIMEOUT", "5"))
        if not self.public_key or len(self.internal_key) < 32:
            raise RuntimeError("Run scripts/configure.py first: JWT_PUBLIC_KEY / INTERNAL_API_KEY missing")


class Database:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)

    def connect(self):
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def init(self, schema: str):
        with self.connect() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript("""
            CREATE TABLE IF NOT EXISTS outbox (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
                type TEXT NOT NULL, payload TEXT NOT NULL, request_id TEXT NOT NULL,
                created_at TEXT NOT NULL, published_at TEXT);
            CREATE TABLE IF NOT EXISTS inbox (id TEXT PRIMARY KEY, received_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS failed_events (
                id TEXT PRIMARY KEY, payload TEXT NOT NULL, error TEXT NOT NULL, created_at TEXT NOT NULL);
            """ + schema)

    @contextmanager
    def transaction(self):
        c = self.connect()
        try:
            # Serialize validation and mutation. Required for overlap protection.
            c.execute("BEGIN IMMEDIATE")
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    @contextmanager
    def read(self):
        c = self.connect()
        try:
            yield c
        finally:
            c.close()


def emit(c, event_type: str, payload: dict):
    c.execute("INSERT INTO outbox(id,type,payload,request_id,created_at) VALUES(?,?,?,?,?)",
              (uid(), event_type, json.dumps(payload, ensure_ascii=False), request_id.get(), stamp()))


def apply_event(app, event: dict):
    """Local mutation and inbox receipt commit atomically, before broker ACK."""
    token = request_id.set(event.get("request_id", "event"))
    try:
        with app.state.db.transaction() as c:
            if c.execute("SELECT 1 FROM inbox WHERE id=?", (event["id"],)).fetchone():
                return False
            app.state.handle_event(c, event)
            c.execute("INSERT INTO inbox VALUES(?,?)", (event["id"], stamp()))
        app.state.log.info("Event processed", extra={"event_id": event["id"]})
        return True
    finally:
        request_id.reset(token)


def install_web(app: FastAPI, service: str):
    # Ship Swagger assets via an installed package, so docs also work offline.
    from swagger_ui_bundle import swagger_ui_path
    app.mount("/swagger-assets", StaticFiles(directory=swagger_ui_path), name="swagger-assets")

    @app.get("/docs", include_in_schema=False)
    def docs():
        return get_swagger_ui_html(openapi_url="/openapi.json", title=service + " API",
            swagger_js_url="/swagger-assets/swagger-ui-bundle.js",
            swagger_css_url="/swagger-assets/swagger-ui.css", swagger_favicon_url="")


def install_errors_and_logs(app, log):
    def error_response(status, code, message, details=None):
        return JSONResponse(status_code=status, content={"error": {
            "code": code, "message": message, "request_id": request_id.get(),
            **({"details": details} if details else {})}})

    @app.middleware("http")
    async def trace(request, call_next):
        incoming = request.headers.get("X-Request-ID", "")
        rid = incoming if re.fullmatch(r"[A-Za-z0-9_-]{8,80}", incoming) else uid()
        token = request_id.set(rid)
        request.state.request_id = rid
        started = time.monotonic()
        try:
            try:
                response = await call_next(request)
            except Exception:
                log.exception("Unexpected server error")
                response = error_response(500, "INTERNAL_ERROR", "Внутренняя ошибка сервиса")
            response.headers["X-Request-ID"] = rid
            response.headers["X-Content-Type-Options"] = "nosniff"
            log.info("HTTP request", extra={"method": request.method, "path": request.url.path,
                     "status": response.status_code,
                     "elapsed_ms": round((time.monotonic()-started)*1000, 2)})
            return response
        finally:
            request_id.reset(token)

    @app.exception_handler(DomainError)
    async def domain_error(request, exc):
        return error_response(exc.status, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return error_response(422, "VALIDATION_ERROR", "Проверьте введённые данные",
                              [{"field": list(e["loc"]), "message": e["msg"]} for e in exc.errors()])

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request, exc):
        return error_response(exc.status_code, "HTTP_ERROR", str(exc.detail))

    @app.exception_handler(sqlite3.Error)
    async def database_error(request, exc):
        log.exception("Database operation failed")
        if isinstance(exc, sqlite3.IntegrityError):
            return error_response(409, "CONFLICT", "Операция противоречит текущему состоянию данных")
        return error_response(503, "DATABASE_UNAVAILABLE", "Хранилище временно недоступно")

    @app.exception_handler(Exception)
    async def unexpected_error(request, exc):
        log.exception("Unexpected server error")
        return error_response(500, "INTERNAL_ERROR", "Внутренняя ошибка сервиса")


def make_app(service, schema, settings=None, handle_event=None, periodic=None):
    config = settings or Settings(service)
    db = Database(config.db)
    db.init(schema)
    log = logger(service)

    @asynccontextmanager
    async def lifespan(app):
        from parking_common.events import EventWorker
        worker = EventWorker(app, getattr(app.state, "periodic", periodic))
        app.state.worker = worker
        await worker.start()
        yield
        await worker.stop()
        app.state.http.close()

    app = FastAPI(title=f"ParkSlot - {service}", version="1.1.0", docs_url=None,
                  redoc_url=None, lifespan=lifespan)
    app.state.config, app.state.db, app.state.log = config, db, log
    app.state.handle_event = handle_event or (lambda c, e: None)
    app.state.http = httpx.Client(timeout=config.timeout, trust_env=False)
    app.state.bus_ready = config.testing
    install_errors_and_logs(app, log)
    install_web(app, service)

    @app.get("/health", tags=["Health"])
    def health():
        return {"status": "ok", "service": service}

    @app.get("/ready", tags=["Health"])
    def ready():
        with db.read() as c:
            c.execute("SELECT 1").fetchone()
        if not app.state.bus_ready:
            fail(503, "BROKER_UNAVAILABLE", "Сервис запущен, соединение с очередью восстанавливается")
        return {"status": "ready", "service": service, "database": "ok", "broker": "ok"}

    return app


bearer = HTTPBearer(auto_error=False)


def decode_actor(config, token):
    try:
        actor = jwt.decode(token, base64.b64decode(config.public_key), algorithms=["EdDSA"],
                           audience="parking-api", issuer="parking-auth",
                           options={"require": ["exp", "iat", "sub", "role", "email"]})
        if actor["role"] not in ("user", "admin"):
            raise jwt.InvalidTokenError("role")
        return actor
    except (jwt.InvalidTokenError, ValueError, TypeError):
        fail(401, "INVALID_TOKEN", "Войдите в систему: токен недействителен или истёк")


def actor(request: Request, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
    if not credentials or credentials.scheme.lower() != "bearer":
        fail(401, "AUTH_REQUIRED", "Необходим вход в систему")
    return decode_actor(request.app.state.config, credentials.credentials)


Actor = Annotated[dict, Depends(actor)]


def admin(current: Actor):
    if current["role"] != "admin":
        fail(403, "FORBIDDEN", "Доступно только администратору")
    return current


Admin = Annotated[dict, Depends(admin)]


def internal(request: Request):
    supplied = request.headers.get("X-Internal-Key", "")
    if not hmac.compare_digest(supplied, request.app.state.config.internal_key):
        fail(401, "INTERNAL_AUTH_REQUIRED", "Необходима авторизация сервиса")


Internal = Annotated[None, Depends(internal)]


def remote(app, service, path, request=None, method="GET", internal_call=False, json_body=None):
    headers = {"X-Request-ID": request_id.get()}
    if internal_call:
        headers["X-Internal-Key"] = app.state.config.internal_key
    elif request:
        headers["Authorization"] = request.headers.get("Authorization", "")
    try:
        response = app.state.http.request(method, app.state.config.urls[service] + path,
                                         headers=headers, json=json_body)
    except httpx.TimeoutException:
        fail(504, "UPSTREAM_TIMEOUT", f"Сервис {service} не ответил вовремя")
    except httpx.RequestError:
        fail(503, "UPSTREAM_UNAVAILABLE", f"Сервис {service} временно недоступен")
    if response.status_code >= 400:
        try:
            detail = response.json()["error"]
        except (ValueError, KeyError, TypeError):
            fail(502, "UPSTREAM_ERROR", f"Ошибка ответа сервиса {service}")
        fail(response.status_code, detail.get("code", "UPSTREAM_ERROR"),
             detail.get("message", "Ошибка зависимого сервиса"))
    try:
        return response.json()
    except ValueError:
        fail(502, "UPSTREAM_ERROR", f"Некорректный ответ сервиса {service}")


def hash_password(password):
    salt = secrets.token_bytes(16)
    hashed = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
    return salt.hex() + ":" + hashed.hex()


def verify_password(password, stored):
    try:
        salt, expected = stored.split(":")
        actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1, dklen=32)
        return hmac.compare_digest(actual.hex(), expected)
    except (ValueError, TypeError):
        return False


def interval(start: datetime, end: datetime):
    if start.tzinfo is None or end.tzinfo is None:
        fail(422, "TIMEZONE_REQUIRED", "Укажите часовой пояс времени")
    start, end = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
    seconds = (end-start).total_seconds()
    if start <= now() or seconds < 900 or seconds > 86400:
        fail(422, "INVALID_INTERVAL", "Бронирование: начало в будущем, длительность от 15 минут до 24 часов")
    return stamp(start), stamp(end), math.ceil(seconds/3600)
