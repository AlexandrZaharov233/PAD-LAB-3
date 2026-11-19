"""Transactional outbox + durable JetStream consumers + local inbox deduplication."""
import asyncio
import json

import nats
from nats.errors import TimeoutError as NatsTimeout
from nats.js.api import AckPolicy, ConsumerConfig, StorageType, StreamConfig
from nats.js.errors import NotFoundError

from parking_common.runtime import apply_event, stamp


class EventWorker:
    def __init__(self, app, periodic=None):
        self.app, self.periodic = app, periodic
        self.tasks, self.nc = [], None

    async def start(self):
        if self.app.state.config.testing:
            return
        self.tasks = [asyncio.create_task(self.run())]

    async def stop(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.nc and not self.nc.is_closed:
            await self.nc.close()

    async def run(self):
        app, cfg = self.app, self.app.state.config
        while True:
            try:
                async def disconnected():
                    app.state.bus_ready = False

                async def reconnected():
                    app.state.bus_ready = True

                async def on_error(error):
                    app.state.log.warning("Broker connection error: %s", type(error).__name__)

                self.nc = await nats.connect(cfg.nats_url, user=cfg.nats_user,
                    password=cfg.nats_password, connect_timeout=2, max_reconnect_attempts=-1,
                    reconnect_time_wait=1, disconnected_cb=disconnected,
                    reconnected_cb=reconnected, error_cb=on_error)
                js = self.nc.jetstream()
                try:
                    await js.stream_info("PARKING")
                except NotFoundError:
                    try:
                        await js.add_stream(config=StreamConfig(name="PARKING",
                            subjects=["parking.events.>"], storage=StorageType.FILE,
                            max_age=7*86400, max_bytes=128*1024*1024))
                    except Exception:
                        # Another service may have created exactly this stream concurrently.
                        await js.stream_info("PARKING")
                subscription = await js.pull_subscribe("parking.events.>",
                    durable=cfg.service + "-events", stream="PARKING",
                    config=ConsumerConfig(ack_policy=AckPolicy.EXPLICIT, ack_wait=10,
                                          max_deliver=8, max_ack_pending=50))
                app.state.bus_ready = True
                app.state.log.info("Connected to persistent event stream")
                while self.nc.is_connected:
                    if self.periodic:
                        await asyncio.to_thread(self.periodic)
                    with app.state.db.read() as c:
                        records = c.execute("SELECT * FROM outbox WHERE published_at IS NULL ORDER BY seq LIMIT 20").fetchall()
                    for row in records:
                        event = {"id": row["id"], "type": row["type"], "payload": json.loads(row["payload"]),
                                 "request_id": row["request_id"], "created_at": row["created_at"],
                                 "producer": cfg.service, "version": 1}
                        await js.publish("parking.events." + row["type"], json.dumps(event).encode(),
                                         headers={"Nats-Msg-Id": row["id"]})
                        with app.state.db.transaction() as c:
                            c.execute("UPDATE outbox SET published_at=? WHERE id=?", (stamp(), row["id"]))
                    try:
                        messages = await subscription.fetch(batch=20, timeout=0.35)
                    except (NatsTimeout, asyncio.TimeoutError):
                        continue
                    for message in messages:
                        try:
                            event = json.loads(message.data)
                            apply_event(app, event)
                        except Exception as exc:
                            app.state.log.exception("Event handler failed")
                            if message.metadata.num_delivered >= 7:
                                with app.state.db.transaction() as c:
                                    c.execute("INSERT OR IGNORE INTO failed_events VALUES(?,?,?,?)",
                                        (str(message.metadata.sequence.stream), message.data.decode(errors="replace"),
                                         type(exc).__name__, stamp()))
                                await message.term()
                            else:
                                await message.nak(delay=1)
                        else:
                            await message.ack()
                if self.nc and not self.nc.is_closed:
                    await self.nc.close()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                app.state.bus_ready = False
                app.state.log.warning("Broker worker retry: %s", type(exc).__name__)
                if self.nc and not self.nc.is_closed:
                    await self.nc.close()
                await asyncio.sleep(1)
