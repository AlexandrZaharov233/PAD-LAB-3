from fastapi import Request
from pydantic import Field, field_validator

from parking_common.runtime import Actor, Input, emit, fail, make_app, stamp, uid

SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
 user_id TEXT PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL,
 phone TEXT NOT NULL DEFAULT '', notifications_enabled INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS vehicles (
 id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES profiles(user_id),
 plate TEXT NOT NULL, label TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(user_id,plate));
"""


class ProfileUpdate(Input):
    name: str = Field(min_length=2, max_length=80)
    phone: str = Field(default="", max_length=30, pattern=r"^[+\d ()-]*$")
    notifications_enabled: bool = True


class VehicleInput(Input):
    plate: str = Field(min_length=2, max_length=16, pattern=r"^[A-Za-zА-Яа-яЁё0-9 -]+$")
    label: str = Field(min_length=1, max_length=60)

    @field_validator("plate")
    @classmethod
    def normalize_plate(cls, value):
        return " ".join(value.upper().split())


def create_app(settings=None):
    def handle(c, event):
        if event["type"] == "UserRegistered":
            p = event["payload"]
            c.execute("INSERT OR IGNORE INTO profiles(user_id,name,email) VALUES(?,?,?)",
                      (p["user_id"], p["name"], p["email"]))

    app = make_app("profile", SCHEMA, settings, handle_event=handle)

    def ensure(c, current):
        # JWT-verified fallback avoids an eventual-consistency race after signup.
        c.execute("INSERT OR IGNORE INTO profiles(user_id,name,email) VALUES(?,?,?)",
                  (current["sub"], current["name"], current["email"]))

    @app.get("/profiles/me", tags=["Профиль"])
    def me(current: Actor):
        with app.state.db.transaction() as c:
            ensure(c, current)
            result = dict(c.execute("SELECT * FROM profiles WHERE user_id=?", (current["sub"],)).fetchone())
            result["notifications_enabled"] = bool(result["notifications_enabled"])
            return result

    @app.patch("/profiles/me", tags=["Профиль"])
    def update(body: ProfileUpdate, current: Actor):
        with app.state.db.transaction() as c:
            ensure(c, current)
            c.execute("UPDATE profiles SET name=?,phone=?,notifications_enabled=? WHERE user_id=?",
                      (body.name, body.phone, int(body.notifications_enabled), current["sub"]))
            emit(c, "ProfilePreferencesUpdated", {"user_id": current["sub"],
                                                  "notifications_enabled": body.notifications_enabled})
        return me(current)

    @app.get("/profiles/me/vehicles", tags=["Автомобили"])
    def vehicles(current: Actor):
        with app.state.db.read() as c:
            return [dict(x) for x in c.execute("SELECT * FROM vehicles WHERE user_id=? ORDER BY created_at", (current["sub"],))]

    @app.post("/profiles/me/vehicles", status_code=201, tags=["Автомобили"])
    def add_vehicle(body: VehicleInput, current: Actor):
        vehicle_id = uid()
        with app.state.db.transaction() as c:
            ensure(c, current)
            if c.execute("SELECT 1 FROM vehicles WHERE user_id=? AND plate=?", (current["sub"], body.plate)).fetchone():
                fail(409, "VEHICLE_EXISTS", "Такой автомобиль уже добавлен")
            c.execute("INSERT INTO vehicles VALUES(?,?,?,?,?)",
                      (vehicle_id, current["sub"], body.plate, body.label, stamp()))
            return dict(c.execute("SELECT * FROM vehicles WHERE id=?", (vehicle_id,)).fetchone())

    @app.get("/profiles/me/vehicles/{vehicle_id}", tags=["Автомобили"])
    def get_vehicle(vehicle_id: str, current: Actor):
        with app.state.db.read() as c:
            row = c.execute("SELECT * FROM vehicles WHERE id=? AND user_id=?", (vehicle_id, current["sub"])).fetchone()
            if not row:
                fail(404, "VEHICLE_NOT_FOUND", "Автомобиль не найден")
            return dict(row)

    @app.delete("/profiles/me/vehicles/{vehicle_id}", tags=["Автомобили"])
    def delete_vehicle(vehicle_id: str, current: Actor):
        with app.state.db.transaction() as c:
            if not c.execute("DELETE FROM vehicles WHERE id=? AND user_id=?", (vehicle_id, current["sub"])).rowcount:
                fail(404, "VEHICLE_NOT_FOUND", "Автомобиль не найден")
        return {"deleted": True}

    return app


if __name__ != "__unit__":
    app = create_app()
