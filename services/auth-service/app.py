import base64
import re
from datetime import timedelta

import jwt
from fastapi import Request
from pydantic import Field, field_validator

from parking_common.runtime import (Actor, Input, Settings, emit, fail, hash_password,
                                    make_app, now, stamp, uid, verify_password)

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
 id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,
 name TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('user','admin')), created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS login_attempts (
 email TEXT PRIMARY KEY, failures INTEGER NOT NULL, blocked_until REAL NOT NULL);
"""


class Credentials(Input):
    email: str = Field(min_length=5, max_length=150)
    password: str = Field(min_length=10, max_length=128)

    @field_validator("email")
    @classmethod
    def email_valid(cls, value):
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
            raise ValueError("Введите адрес электронной почты")
        return value.lower()


class Registration(Credentials):
    name: str = Field(min_length=2, max_length=80)


def create_app(settings=None):
    app = make_app("auth", SCHEMA, settings)
    cfg = app.state.config
    if not cfg.private_key:
        raise RuntimeError("JWT_PRIVATE_KEY is required only by auth service")
    with app.state.db.transaction() as c:
        if not c.execute("SELECT 1 FROM accounts WHERE email=?", (cfg.admin_email,)).fetchone():
            if len(cfg.admin_password) < 12:
                raise RuntimeError("ADMIN_PASSWORD must contain at least 12 characters")
            admin_id = uid()
            c.execute("INSERT INTO accounts VALUES(?,?,?,?,?,?)",
                (admin_id, cfg.admin_email, hash_password(cfg.admin_password), "Администратор", "admin", stamp()))
            emit(c, "UserRegistered", {"user_id": admin_id, "email": cfg.admin_email, "name": "Администратор"})

    def token_for(row):
        payload = {"sub": row["id"], "email": row["email"], "name": row["name"],
                   "role": row["role"], "iat": now(), "exp": now()+timedelta(hours=2),
                   "iss": "parking-auth", "aud": "parking-api"}
        return {"access_token": jwt.encode(payload, base64.b64decode(cfg.private_key), algorithm="EdDSA"),
                "token_type": "bearer", "expires_in": 7200,
                "user": {k: row[k] for k in ("id", "email", "name", "role")}}

    @app.post("/auth/register", status_code=201, tags=["Учётные записи"])
    def register(body: Registration):
        user_id, created = uid(), stamp()
        password_hash = hash_password(body.password)
        with app.state.db.transaction() as c:
            if c.execute("SELECT 1 FROM accounts WHERE email=?", (body.email,)).fetchone():
                fail(409, "EMAIL_EXISTS", "Этот адрес уже зарегистрирован")
            c.execute("INSERT INTO accounts VALUES(?,?,?,?,?,?)",
                      (user_id, body.email, password_hash, body.name, "user", created))
            emit(c, "UserRegistered", {"user_id": user_id, "email": body.email, "name": body.name})
            row = dict(c.execute("SELECT * FROM accounts WHERE id=?", (user_id,)).fetchone())
        return token_for(row)

    @app.post("/auth/login", tags=["Учётные записи"])
    def login(body: Credentials):
        with app.state.db.read() as c:
            attempt = c.execute("SELECT * FROM login_attempts WHERE email=?", (body.email,)).fetchone()
            if attempt and attempt["blocked_until"] > now().timestamp():
                fail(429, "LOGIN_BLOCKED", "Слишком много попыток. Повторите через минуту")
            row = c.execute("SELECT * FROM accounts WHERE email=?", (body.email,)).fetchone()
        if not row or not verify_password(body.password, row["password_hash"]):
            # Failure counter is committed before the HTTP error is raised.
            with app.state.db.transaction() as c:
                c.execute("INSERT INTO login_attempts VALUES(?,1,0) ON CONFLICT(email) DO UPDATE SET failures=failures+1",
                          (body.email,))
                failures = c.execute("SELECT failures FROM login_attempts WHERE email=?", (body.email,)).fetchone()[0]
                if failures >= 8:
                    c.execute("UPDATE login_attempts SET failures=0,blocked_until=? WHERE email=?",
                              (now().timestamp()+60, body.email))
            fail(401, "INVALID_CREDENTIALS", "Неверный адрес или пароль")
        with app.state.db.transaction() as c:
            c.execute("DELETE FROM login_attempts WHERE email=?", (body.email,))
        return token_for(row)

    @app.get("/auth/me", tags=["Учётные записи"])
    def me(current: Actor):
        return {"id": current["sub"], "email": current["email"], "name": current["name"], "role": current["role"]}

    return app


if __name__ != "__unit__":
    app = create_app()
