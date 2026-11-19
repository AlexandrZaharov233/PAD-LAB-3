from parking_common.runtime import Actor, fail, make_app, stamp, uid

SCHEMA = """
CREATE TABLE IF NOT EXISTS notifications (
 id TEXT PRIMARY KEY, user_id TEXT NOT NULL, event_id TEXT UNIQUE NOT NULL,
 type TEXT NOT NULL, title TEXT NOT NULL, message TEXT NOT NULL,
 booking_id TEXT, is_read INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS notifications_owner ON notifications(user_id,created_at);
CREATE TABLE IF NOT EXISTS preferences(user_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1);
"""

TEMPLATES = {
 "BookingCreated": ("Место зарезервировано", "Оплатите бронирование в течение 10 минут."),
 "BookingConfirmed": ("Бронирование подтверждено", "Оплата обработана. Парковочное место закреплено за вами."),
 "BookingCancelled": ("Бронирование отменено", "Место освобождено. Успешный учебный платёж будет возвращён автоматически."),
 "BookingExpired": ("Время оплаты истекло", "Бронирование освобождено. Можно выбрать место повторно."),
 "PaymentDeclined": ("Учебная оплата отклонена", "Можно повторить оплату до истечения срока бронирования."),
 "PaymentRefunded": ("Учебный платёж возвращён", "Возврат зарегистрирован в истории платежей."),
 "WaitlistOfferCreated": ("Освободилось парковочное место", "Откройте лист ожидания, чтобы забронировать место."),
}


def create_app(settings=None):
    def handle(c, event):
        if event["type"] == "ProfilePreferencesUpdated":
            p = event["payload"]
            c.execute("INSERT INTO preferences VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET enabled=excluded.enabled",
                      (p["user_id"], int(p["notifications_enabled"])))
            return
        if event["type"] not in TEMPLATES:
            return
        p = event["payload"]
        preference = c.execute("SELECT enabled FROM preferences WHERE user_id=?", (p["user_id"],)).fetchone()
        if preference and not preference[0]:
            return
        title, text = TEMPLATES[event["type"]]
        if event["type"] == "WaitlistOfferCreated":
            text = f"{p['lot_name']}, место {p['space_code']}. Откройте лист ожидания, чтобы забронировать."
        c.execute("INSERT OR IGNORE INTO notifications VALUES(?,?,?,?,?,?,?,0,?)",
                  (uid(), p["user_id"], event["id"], event["type"], title, text,
                   p.get("booking_id"), stamp()))

    app = make_app("notification", SCHEMA, settings, handle_event=handle)

    @app.get("/notifications", tags=["Уведомления"])
    def mine(current: Actor):
        with app.state.db.read() as c:
            return [dict(x) for x in c.execute("SELECT * FROM notifications WHERE user_id=? ORDER BY created_at DESC LIMIT 200",
                                              (current["sub"],))]

    @app.post("/notifications/{notification_id}/read", tags=["Уведомления"])
    def read(notification_id: str, current: Actor):
        with app.state.db.transaction() as c:
            if not c.execute("UPDATE notifications SET is_read=1 WHERE id=? AND user_id=?", (notification_id, current["sub"])).rowcount:
                fail(404, "NOTIFICATION_NOT_FOUND", "Уведомление не найдено")
        return {"read": True}

    return app


if __name__ != "__unit__":
    app = create_app()
