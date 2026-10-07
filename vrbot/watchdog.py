"""Self-watchdog: a stuck bot exits so its supervisor (Docker restart policy, the Windows manager) starts it fresh.

Docker only RESTARTS containers that exit; an "unhealthy" container keeps running. So the bot checks itself from a
plain thread (independent of the asyncio loop it watches):

* the event loop stopped ticking for LOOP_STUCK seconds  -> exit (a frozen process can't recover by itself);
* not connected to Discord for DISCONNECTED seconds       -> exit (discord.py normally reconnects on its own; this
  only catches the rare case where it never does). The limit is long on purpose: during a real Discord or internet
  outage a restart can't help, and the supervisor's back-off keeps restarts slow.

Before exiting it records why (data/watchdog.json + the database), so the downtime report names the reason.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path

log = logging.getLogger("vrbot.watchdog")
LOOP_STUCK = 300
DISCONNECTED = 900
CHECK_EVERY = 15


def should_exit(now: float, last_tick: float, connected_since_or_none: float | None, last_connected: float,
                loop_stuck: float = LOOP_STUCK, disconnected: float = DISCONNECTED) -> str | None:
    """Pure decision: None = healthy, else the reason to exit."""
    if now - last_tick > loop_stuck:
        return f"event loop stuck for {int(now - last_tick)} s"
    if connected_since_or_none is None and now - last_connected > disconnected:
        return f"not connected to Discord for {int(now - last_connected)} s"
    return None


class Watchdog:
    def __init__(self, data_dir: Path, db_file: Path):
        self.data_dir, self.db_file = Path(data_dir), Path(db_file)
        self.last_tick = time.time()
        self.connected: float | None = None
        self.last_connected = time.time()        # grace period after start
        self._stop = threading.Event()

    # called from the bot (event loop) ------------------------------------------------
    def tick(self, connected: bool) -> None:
        now = time.time()
        self.last_tick = now
        if connected:
            self.connected = self.connected or now
            self.last_connected = now
        else:
            self.connected = None

    def stop(self) -> None:
        self._stop.set()

    # thread ---------------------------------------------------------------------------
    def start(self) -> None:
        threading.Thread(target=self._run, name="watchdog", daemon=True).start()

    def _run(self) -> None:
        while not self._stop.wait(CHECK_EVERY):
            why = should_exit(time.time(), self.last_tick, self.connected, self.last_connected)
            if why:
                self._record(why)
                log.critical("watchdog: %s — exiting so the supervisor restarts the bot", why)
                logging.shutdown()
                os._exit(3)

    def _record(self, why: str) -> None:
        try:
            (self.data_dir / "watchdog.json").write_text(json.dumps({"at": time.time(), "reason": why}))
        except OSError:
            pass
        try:   # synchronous write: the asyncio loop may be the thing that is stuck
            with sqlite3.connect(self.db_file, timeout=5) as c:
                c.execute("INSERT INTO kv (key, value) VALUES ('uptime:shutdown', ?) "
                          "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                          (json.dumps({"at": time.time(), "kind": "watchdog", "detail": why}),))
            c.close()
        except sqlite3.Error:
            pass
