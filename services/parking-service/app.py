from pydantic import Field

from parking_common.runtime import Admin, Input, fail, make_app, stamp, uid

SCHEMA = """
CREATE TABLE IF NOT EXISTS lots (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, address TEXT NOT NULL, description TEXT NOT NULL,
 hourly_rate_cents INTEGER NOT NULL CHECK(hourly_rate_cents>0), currency TEXT NOT NULL DEFAULT 'MDL',
 active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS spaces (
 id TEXT PRIMARY KEY, lot_id TEXT NOT NULL REFERENCES lots(id), code TEXT NOT NULL,
 kind TEXT NOT NULL DEFAULT 'standard', active INTEGER NOT NULL DEFAULT 1, UNIQUE(lot_id,code));
"""


class LotInput(Input):
    name: str = Field(min_length=3, max_length=100)
    address: str = Field(min_length=5, max_length=200)
    description: str = Field(default="", max_length=500)
    hourly_rate_cents: int = Field(ge=100, le=100000)
    capacity: int = Field(ge=1, le=200)


class LotUpdate(Input):
    hourly_rate_cents: int = Field(ge=100, le=100000)
    active: bool = True


class SpaceUpdate(Input):
    active: bool


def create_app(settings=None):
    app = make_app("parking", SCHEMA, settings)

    def create_lot(c, body):
        lot_id = uid()
        c.execute("INSERT INTO lots VALUES(?,?,?,?,?,'MDL',1,?)",
                  (lot_id, body.name, body.address, body.description, body.hourly_rate_cents, stamp()))
        for number in range(1, body.capacity+1):
            c.execute("INSERT INTO spaces VALUES(?,?,?,?,1)",
                      (uid(), lot_id, f"P{number:02}", "ev" if number % 5 == 0 else "standard"))
        return lot_id

    with app.state.db.transaction() as c:
        if not c.execute("SELECT 1 FROM lots").fetchone():
            for body in [LotInput(name="Центральная", address="Учебная улица, 10", description="Открытая парковка, круглосуточно", hourly_rate_cents=3000, capacity=12),
                         LotInput(name="Университет", address="Студенческая улица, 4", description="Парковка рядом с кампусом, круглосуточно", hourly_rate_cents=2000, capacity=8),
                         LotInput(name="Бизнес-квартал", address="Деловая улица, 21", description="Крытая парковка, круглосуточно", hourly_rate_cents=4500, capacity=10)]:
                create_lot(c, body)

    def lot_dict(row):
        item = dict(row)
        item["active"] = bool(item["active"])
        return item

    @app.get("/parking/lots", tags=["Парковки"])
    def lots():
        with app.state.db.read() as c:
            return [lot_dict(x) for x in c.execute("""SELECT l.*,COUNT(s.id) AS capacity,
               SUM(CASE WHEN s.active=1 THEN 1 ELSE 0 END) AS active_spaces
               FROM lots l LEFT JOIN spaces s ON s.lot_id=l.id GROUP BY l.id ORDER BY l.name""")]

    @app.get("/parking/lots/{lot_id}", tags=["Парковки"])
    def lot(lot_id: str):
        with app.state.db.read() as c:
            row = c.execute("SELECT * FROM lots WHERE id=?", (lot_id,)).fetchone()
            if not row:
                fail(404, "LOT_NOT_FOUND", "Парковка не найдена")
            return lot_dict(row)

    @app.get("/parking/lots/{lot_id}/spaces", tags=["Места"])
    def spaces(lot_id: str):
        lot(lot_id)
        with app.state.db.read() as c:
            return [lot_dict(x) for x in c.execute("SELECT * FROM spaces WHERE lot_id=? ORDER BY code", (lot_id,))]

    @app.get("/parking/spaces/{space_id}", tags=["Места"])
    def space(space_id: str):
        with app.state.db.read() as c:
            row = c.execute("""SELECT s.id,s.lot_id,s.code,s.kind,s.active,l.name AS lot_name,
               l.address,l.hourly_rate_cents,l.currency,l.active AS lot_active
               FROM spaces s JOIN lots l ON l.id=s.lot_id WHERE s.id=?""", (space_id,)).fetchone()
            if not row:
                fail(404, "SPACE_NOT_FOUND", "Парковочное место не найдено")
            return lot_dict(row)

    @app.post("/parking/lots", status_code=201, tags=["Администрирование"])
    def add_lot(body: LotInput, current: Admin):
        with app.state.db.transaction() as c:
            lot_id = create_lot(c, body)
        return lot(lot_id)

    @app.patch("/parking/lots/{lot_id}", tags=["Администрирование"])
    def update_lot(lot_id: str, body: LotUpdate, current: Admin):
        with app.state.db.transaction() as c:
            if not c.execute("UPDATE lots SET hourly_rate_cents=?,active=? WHERE id=?",
                             (body.hourly_rate_cents, int(body.active), lot_id)).rowcount:
                fail(404, "LOT_NOT_FOUND", "Парковка не найдена")
        return lot(lot_id)

    @app.patch("/parking/spaces/{space_id}", tags=["Администрирование"])
    def update_space(space_id: str, body: SpaceUpdate, current: Admin):
        with app.state.db.transaction() as c:
            if not c.execute("UPDATE spaces SET active=? WHERE id=?", (int(body.active), space_id)).rowcount:
                fail(404, "SPACE_NOT_FOUND", "Парковочное место не найдено")
        return space(space_id)

    return app


if __name__ != "__unit__":
    app = create_app()
