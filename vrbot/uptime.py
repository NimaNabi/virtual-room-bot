"""Downtime detection and database backup policy (pure logic, no Discord).

Downtime is measured from the last moment the bot PROVABLY had a working Discord connection: the last heartbeat that
Discord acknowledged. Not "now" — after a network drop a dead connection can still look connected for over a minute,
and using the current clock would make outages look shorter than they were.
"""
from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

REASONS = {
    "connection": "connection to Discord lost (network, VPN or Discord problem); the bot kept running",
    "clean": "the bot was stopped or restarted (update, restart or shutdown command)",
    "unknown": "no clean shutdown was recorded: the machine was off or asleep, lost power, or the bot crashed",
}


@dataclass
class Downtime:
    start: float      # epoch seconds: last proven connection
    end: float        # epoch seconds: connected again
    reason: str       # key of REASONS

    @property
    def seconds(self) -> int:
        return int(self.end - self.start)

    def as_dict(self) -> dict:
        return {"from": self.start, "to": self.end, "seconds": self.seconds, "reason": self.reason}


def classify(last_alive: float | None, shutdown: dict | None, process_started: float, now: float,
             min_gap: float = 180.0) -> Downtime | None:
    """Was there a meaningful outage before this (re)connection?

    last_alive: last acknowledged heartbeat (persisted). shutdown: {"at", "kind"} written on a clean stop.
    process_started: when THIS process started. Gaps up to `min_gap` (a quick restart, a short reconnect) are ignored.
    """
    if not last_alive or now - last_alive <= min_gap:
        return None
    if last_alive >= process_started:
        reason = "connection"          # this same process saved the last heartbeat: it never stopped
    elif shutdown and shutdown.get("at", 0) >= last_alive:
        reason = shutdown.get("kind") if shutdown.get("kind") in REASONS else "clean"
    else:
        reason = "unknown"
    return Downtime(last_alive, now, reason)


def human_duration(seconds: int) -> str:
    m = max(1, round(seconds / 60))
    d, rem = divmod(m, 1440)
    h, mm = divmod(rem, 60)
    if d:
        return f"{d} d {h} h" if h else f"{d} d"
    if h:
        return f"{h} h {mm} min" if mm else f"{h} h"
    return f"{mm} min"


# ---------------------------------------------------------------- scheduled database backups
BACKUP_RE = re.compile(r"^auto-(\d{8}-\d{6})\.db$")


def list_backups(folder: Path) -> list[Path]:
    """Scheduled/manual database backups made by the bot, newest first."""
    if not folder.exists():
        return []
    return sorted((p for p in folder.iterdir() if BACKUP_RE.match(p.name)), key=lambda p: p.name, reverse=True)


def backup_name(now: datetime) -> str:
    return f"auto-{now.strftime('%Y%m%d-%H%M%S')}.db"


def backup_due(newest: datetime | None, now: datetime, hour: int) -> bool:
    """Nightly at `hour` (local time of the host), plus ONE catch-up when the last backup is over a day old
    (the machine was off at backup time)."""
    if newest is None:
        return True
    if now - newest >= timedelta(hours=26):
        return True
    return now.hour == hour and newest.date() < now.date()


def next_backup(newest: datetime | None, now: datetime, hour: int) -> datetime:
    if backup_due(newest, now, hour):
        return now
    nxt = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if nxt <= now or (newest and newest.date() >= nxt.date()):
        nxt += timedelta(days=1)
    return nxt


def backup_time(path: Path) -> datetime | None:
    m = BACKUP_RE.match(path.name)
    return datetime.strptime(m.group(1), "%Y%m%d-%H%M%S") if m else None


def prune(folder: Path, keep: int) -> list[Path]:
    removed = []
    for old in list_backups(folder)[max(1, keep):]:
        old.unlink(missing_ok=True)
        removed.append(old)
    return removed


def snapshot_sqlite(src: Path, dest: Path) -> int:
    """Consistent copy of a live SQLite database (SQLite's online backup API), verified with an integrity check.
    Written to a temporary name first, so a half-written file is never mistaken for a backup."""
    from contextlib import closing
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".partial")
    _remove_with_sidecars(tmp)
    with closing(sqlite3.connect(src)) as s, closing(sqlite3.connect(tmp)) as d:
        s.backup(d)
        d.execute("PRAGMA journal_mode=DELETE")   # a self-contained single file (no -wal/-shm next to it)
        ok = d.execute("PRAGMA integrity_check").fetchone()[0]
    if ok != "ok":
        _remove_with_sidecars(tmp)
        raise RuntimeError(f"backup failed its integrity check: {ok}")
    os.replace(tmp, dest)
    _remove_with_sidecars(tmp)
    return dest.stat().st_size


def _remove_with_sidecars(p: Path) -> None:
    for x in (p, p.with_name(p.name + "-wal"), p.with_name(p.name + "-shm"), p.with_name(p.name + "-journal")):
        x.unlink(missing_ok=True)


def clean_leftovers(folder: Path) -> int:
    """Remove interrupted/partial backup files (never finished backups or their SQLite side files)."""
    n = 0
    for p in folder.glob("auto-*.partial*") if folder.exists() else []:
        p.unlink(missing_ok=True)
        n += 1
    return n


def local_now() -> datetime:
    """Host-local wall clock (set TZ for the container; nothing is hard-coded)."""
    return datetime.now()


def utc_ts() -> float:
    return datetime.now(timezone.utc).timestamp()
