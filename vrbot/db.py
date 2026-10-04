"""SQLite persistence (WAL) with ordered SQL migrations."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite

log = logging.getLogger("vrbot.db")
MIGRATIONS = Path(__file__).parent / "migrations"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


_DUR = re.compile(r"^\s*(\d+)\s*(s|m|h|d|w)\s*$", re.I)


def parse_duration(text: str) -> timedelta:
    """'30m', '12h', '7d', '2w' -> timedelta. Combined forms like '1d12h' also work."""
    text = text.strip().lower()
    parts = re.findall(r"(\d+)\s*([smhdw])", text)
    if not parts or "".join(n + u for n, u in parts) != text.replace(" ", ""):
        raise ValueError(f"Bad duration {text!r}; use e.g. 30m, 12h, 7d, 2w")
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    return timedelta(seconds=sum(int(n) * mult[u] for n, u in parts))


@dataclass
class LogQuery:
    guild_id: int | None = None
    types: list[str] = field(default_factory=list)
    category: str | None = None
    user_id: int | None = None       # matches target OR actor
    target_id: int | None = None
    actor_id: int | None = None
    channel_id: int | None = None
    since: str | None = None         # iso
    until: str | None = None
    text: str | None = None
    limit: int = 25
    offset: int = 0

    def build(self) -> tuple[str, list]:
        where, params = [], []
        if self.guild_id is not None:
            where.append("guild_id = ?"); params.append(self.guild_id)
        if self.types:
            where.append(f"type IN ({','.join('?' * len(self.types))})"); params.extend(self.types)
        if self.category:
            where.append("category = ?"); params.append(self.category)
        if self.user_id is not None:
            where.append("(target_id = ? OR actor_id = ?)"); params.extend([self.user_id, self.user_id])
        if self.target_id is not None:
            where.append("target_id = ?"); params.append(self.target_id)
        if self.actor_id is not None:
            where.append("actor_id = ?"); params.append(self.actor_id)
        if self.channel_id is not None:
            where.append("channel_id = ?"); params.append(self.channel_id)
        if self.since:
            where.append("ts >= ?"); params.append(self.since)
        if self.until:
            where.append("ts <= ?"); params.append(self.until)
        if self.text:
            like = f"%{self.text.lower()}%"
            where.append("(lower(coalesce(target_name,'')) LIKE ? OR lower(coalesce(actor_name,'')) LIKE ? OR "
                         "lower(coalesce(channel_name,'')) LIKE ? OR lower(coalesce(role_name,'')) LIKE ? OR "
                         "lower(coalesce(reason,'')) LIKE ? OR lower(type) LIKE ? OR lower(coalesce(details,'')) LIKE ?)")
            params.extend([like] * 7)
        sql = "SELECT * FROM events"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?"
        params.extend([max(1, min(self.limit, 5000)), max(0, self.offset)])
        return sql, params


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self.conn: aiosqlite.Connection | None = None
        self.event_hooks: list = []   # callables(row dict) — e.g. the owner-only log sink
        self.identity_resolver = None  # callable(user_id) -> identity snapshot dict | None (see identity.py)

    async def open(self) -> None:
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("PRAGMA foreign_keys=ON")
        await self.conn.execute("PRAGMA busy_timeout=5000")
        await self.migrate()

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()

    async def migrate(self) -> list[int]:
        assert self.conn
        await self.conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
        async with self.conn.execute("SELECT version FROM schema_version") as cur:
            done = {r[0] for r in await cur.fetchall()}
        applied = []
        for f in sorted(MIGRATIONS.glob("*.sql")):
            v = int(f.name.split("_", 1)[0])
            if v in done:
                continue
            log.info("applying migration %s", f.name)
            await self.conn.executescript(f.read_text(encoding="utf-8"))
            await self.conn.execute("INSERT INTO schema_version VALUES (?, ?)", (v, now_iso()))
            await self.conn.commit()
            applied.append(v)
        return applied

    async def ping(self) -> bool:
        try:
            async with self.conn.execute("SELECT 1") as cur:
                return (await cur.fetchone())[0] == 1
        except Exception:  # noqa: BLE001
            return False

    # ---- events --------------------------------------------------------
    async def add_event(self, *, type: str, category: str, guild_id: int | None = None, ts: str | None = None,
                        target_id=None, target_name=None, actor_id=None, actor_name=None,
                        actor_confidence: str = "unknown", channel_id=None, channel_name=None,
                        role_id=None, role_name=None, reason=None, details: dict | None = None,
                        source: str = "gateway") -> int:
        # global identity contract: every person in an event gets a structured snapshot (ID + names at that time)
        if self.identity_resolver is not None:
            details = dict(details or {})
            for role_, uid in (("target_identity", target_id), ("actor_identity", actor_id)):
                if uid and role_ not in details:
                    try:
                        ident = self.identity_resolver(int(uid))
                    except (TypeError, ValueError):
                        ident = None
                    if ident:
                        details[role_] = ident
            details = details or None
        cur = await self.conn.execute(
            "INSERT INTO events (ts, guild_id, type, category, target_id, target_name, actor_id, actor_name,"
            " actor_confidence, channel_id, channel_name, role_id, role_name, reason, details, source)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts or now_iso(), guild_id, type, category, target_id, target_name, actor_id, actor_name,
             actor_confidence, channel_id, channel_name, role_id, role_name, reason,
             json.dumps(details, default=str) if details else None, source))
        await self.conn.commit()
        if self.event_hooks and source != "audit_backfill":
            row = {"id": cur.lastrowid, "ts": ts or now_iso(), "guild_id": guild_id, "type": type, "category": category,
                   "target_id": target_id, "target_name": target_name, "actor_id": actor_id, "actor_name": actor_name,
                   "actor_confidence": actor_confidence, "channel_id": channel_id, "channel_name": channel_name,
                   "role_id": role_id, "role_name": role_name, "reason": reason,
                   "details": json.dumps(details, default=str) if details else None, "source": source}
            for hook in self.event_hooks:
                try:
                    hook(row)
                except Exception:  # noqa: BLE001 — logging sinks must never break storage
                    log.warning("event hook failed", exc_info=True)
        return cur.lastrowid

    async def query_events(self, q: LogQuery) -> list[dict]:
        sql, params = q.build()
        async with self.conn.execute(sql, params) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def count_events(self, since: str, guild_id: int | None = None) -> dict[str, int]:
        sql = "SELECT type, COUNT(*) FROM events WHERE ts >= ?" + (" AND guild_id = ?" if guild_id else "") + " GROUP BY type"
        async with self.conn.execute(sql, [since] + ([guild_id] if guild_id else [])) as cur:
            return {r[0]: r[1] for r in await cur.fetchall()}

    async def oldest_event_ts(self, guild_id: int) -> str | None:
        async with self.conn.execute("SELECT MIN(ts) FROM events WHERE guild_id = ?", (guild_id,)) as cur:
            r = await cur.fetchone()
        return r[0] if r else None

    async def event_exists(self, source: str, key: str) -> bool:
        async with self.conn.execute("SELECT 1 FROM events WHERE source = ? AND details LIKE ? LIMIT 1",
                                     (source, f'%"audit_id": "{key}"%')) as cur:
            return await cur.fetchone() is not None

    async def prune(self, events_days: int, message_days: int, voice_days: int) -> int:
        now = datetime.now(timezone.utc)
        total = 0
        for sql, days in (
            ("DELETE FROM events WHERE category = 'message' AND ts < ?", message_days),
            ("DELETE FROM events WHERE category = 'voice' AND ts < ?", voice_days),
            ("DELETE FROM events WHERE ts < ?", events_days),
        ):
            cur = await self.conn.execute(sql, (iso(now - timedelta(days=days)),))
            total += cur.rowcount
        await self.conn.commit()
        return total

    # ---- mod cases -----------------------------------------------------
    async def add_case(self, *, guild_id, action, moderator_id, moderator_name, user_id=None, user_name=None,
                       reason=None, duration_s=None, channel_id=None, extra: dict | None = None) -> int:
        cur = await self.conn.execute(
            "INSERT INTO mod_cases (ts, guild_id, action, user_id, user_name, moderator_id, moderator_name, reason,"
            " duration_s, channel_id, extra) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (now_iso(), guild_id, action, user_id, user_name, moderator_id, moderator_name, reason, duration_s,
             channel_id, json.dumps(extra) if extra else None))
        await self.conn.commit()
        return cur.lastrowid

    async def cases_for(self, guild_id: int, user_id: int, action: str | None = None) -> list[dict]:
        sql = "SELECT * FROM mod_cases WHERE guild_id=? AND user_id=?" + (" AND action=?" if action else "") + " ORDER BY ts DESC"
        async with self.conn.execute(sql, [guild_id, user_id] + ([action] if action else [])) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def deactivate_case(self, case_id: int) -> bool:
        cur = await self.conn.execute("UPDATE mod_cases SET active=0 WHERE id=?", (case_id,))
        await self.conn.commit()
        return cur.rowcount > 0

    # ---- snapshots -----------------------------------------------------
    async def add_snapshot(self, guild_id: int, kind: str, data: dict, label: str | None = None,
                           created_by: int | None = None, summary: str | None = None) -> int:
        cur = await self.conn.execute(
            "INSERT INTO snapshots (ts, guild_id, kind, label, created_by, summary, data) VALUES (?,?,?,?,?,?,?)",
            (now_iso(), guild_id, kind, label, created_by, summary, json.dumps(data)))
        await self.conn.commit()
        return cur.lastrowid

    async def list_snapshots(self, guild_id: int, limit: int = 20) -> list[dict]:
        async with self.conn.execute(
                "SELECT id, ts, kind, label, created_by, summary FROM snapshots WHERE guild_id=? ORDER BY id DESC LIMIT ?",
                (guild_id, limit)) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def get_snapshot(self, snap_id: int) -> dict | None:
        async with self.conn.execute("SELECT * FROM snapshots WHERE id=?", (snap_id,)) as cur:
            r = await cur.fetchone()
        if not r:
            return None
        d = dict(r)
        d["data"] = json.loads(d["data"])
        return d

    async def latest_snapshot(self, guild_id: int, before_ts: str | None = None) -> dict | None:
        sql = "SELECT id FROM snapshots WHERE guild_id=?" + (" AND ts <= ?" if before_ts else "") + " ORDER BY ts DESC, id DESC LIMIT 1"
        async with self.conn.execute(sql, [guild_id] + ([before_ts] if before_ts else [])) as cur:
            r = await cur.fetchone()
        return await self.get_snapshot(r[0]) if r else None

    async def prune_snapshots(self, guild_id: int, keep: int) -> int:
        cur = await self.conn.execute(
            "DELETE FROM snapshots WHERE guild_id=? AND kind != 'manual' AND id NOT IN "
            "(SELECT id FROM snapshots WHERE guild_id=? ORDER BY id DESC LIMIT ?)", (guild_id, guild_id, keep))
        await self.conn.commit()
        return cur.rowcount

    # ---- change batches ------------------------------------------------
    async def add_batch(self, guild_id, actor_id, source, status, summary, changes: list[dict], snapshot_id=None) -> int:
        cur = await self.conn.execute(
            "INSERT INTO change_batches (ts, guild_id, actor_id, source, status, summary, changes, snapshot_id)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (now_iso(), guild_id, actor_id, source, status, summary, json.dumps(changes), snapshot_id))
        await self.conn.commit()
        return cur.lastrowid

    async def update_batch(self, batch_id: int, status: str, result: dict | None = None) -> None:
        await self.conn.execute("UPDATE change_batches SET status=?, result=? WHERE id=?",
                                (status, json.dumps(result) if result else None, batch_id))
        await self.conn.commit()

    async def get_batch(self, batch_id: int) -> dict | None:
        async with self.conn.execute("SELECT * FROM change_batches WHERE id=?", (batch_id,)) as cur:
            r = await cur.fetchone()
        if not r:
            return None
        d = dict(r)
        d["changes"] = json.loads(d["changes"])
        return d

    async def list_batches(self, guild_id: int, limit: int = 10) -> list[dict]:
        async with self.conn.execute(
                "SELECT id, ts, actor_id, source, status, summary FROM change_batches WHERE guild_id=? ORDER BY id DESC LIMIT ?",
                (guild_id, limit)) as cur:
            return [dict(r) for r in await cur.fetchall()]

    # ---- kv --------------------------------------------------------------
    async def kv_get(self, key: str, default=None):
        async with self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)) as cur:
            r = await cur.fetchone()
        return json.loads(r[0]) if r else default

    async def kv_set(self, key: str, value) -> None:
        await self.conn.execute("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                (key, json.dumps(value)))
        await self.conn.commit()

    async def stats(self) -> dict:
        out = {}
        for t in ("events", "mod_cases", "snapshots", "change_batches"):
            async with self.conn.execute(f"SELECT COUNT(*) FROM {t}") as cur:  # noqa: S608 (static names)
                out[t] = (await cur.fetchone())[0]
        if self.path != ":memory:":
            out["size_mb"] = round(Path(self.path).stat().st_size / 1_048_576, 2)
        return out
