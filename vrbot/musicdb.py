"""Music history + favourites (pure SQL helpers over the bot's SQLite DB)."""
from __future__ import annotations

from .db import now_iso

MAX_USER_FAVS, MAX_SERVER_FAVS = 25, 15


def track_key(uri: str | None, source: str | None, identifier: str | None) -> str:
    return uri or f"{source}:{identifier}"


async def record_play(db, guild_id: int, *, key: str, title: str, author: str | None, uri: str | None, source: str | None,
                      length_ms: int | None, requester_id: int | None, channel_id: int | None) -> None:
    await db.conn.execute(
        "INSERT INTO music_plays (ts, guild_id, track_key, title, author, uri, source, length_ms, requester_id, channel_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)", (now_iso(), guild_id, key, title[:200], (author or "")[:120], uri, source,
                                         length_ms, requester_id, channel_id))
    await db.conn.commit()


async def recent(db, guild_id: int, limit: int = 10, user_id: int | None = None) -> list[dict]:
    """Distinct recently played tracks (newest first)."""
    sql = ("SELECT track_key, title, author, uri, MAX(ts) AS last, COUNT(*) AS plays FROM music_plays "
           "WHERE guild_id = ? AND uri IS NOT NULL" + (" AND requester_id = ?" if user_id else "") +
           " GROUP BY track_key ORDER BY last DESC LIMIT ?")
    args = (guild_id, user_id, limit) if user_id else (guild_id, limit)
    async with db.conn.execute(sql, args) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def frequent(db, guild_id: int, limit: int = 10, min_plays: int = 2) -> list[dict]:
    async with db.conn.execute(
            "SELECT track_key, title, author, uri, COUNT(*) AS plays, MAX(ts) AS last FROM music_plays "
            "WHERE guild_id = ? AND uri IS NOT NULL GROUP BY track_key HAVING plays >= ? ORDER BY plays DESC, last DESC LIMIT ?",
            (guild_id, min_plays, limit)) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def favorites(db, guild_id: int, user_id: int) -> list[dict]:
    async with db.conn.execute("SELECT * FROM music_favorites WHERE guild_id = ? AND user_id = ? ORDER BY added_ts DESC",
                               (guild_id, user_id)) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def add_favorite(db, guild_id: int, user_id: int, *, key: str, title: str, author: str | None, uri: str,
                       label: str | None = None) -> str:
    cap = MAX_SERVER_FAVS if user_id == 0 else MAX_USER_FAVS
    have = await favorites(db, guild_id, user_id)
    if any(f["track_key"] == key for f in have):
        return "exists"
    if len(have) >= cap:
        return "full"
    await db.conn.execute("INSERT INTO music_favorites VALUES (?,?,?,?,?,?,?,?)",
                          (guild_id, user_id, key, title[:200], (author or "")[:120], uri, label, now_iso()))
    await db.conn.commit()
    return "added"


async def remove_favorite(db, guild_id: int, user_id: int, key: str) -> None:
    await db.conn.execute("DELETE FROM music_favorites WHERE guild_id = ? AND user_id = ? AND track_key = ?",
                          (guild_id, user_id, key))
    await db.conn.commit()
