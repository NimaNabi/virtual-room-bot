"""System health for the owner: downtime reports, scheduled local database backups, server added/removed notices.

* Downtime: every 30 s the last heartbeat Discord ACKNOWLEDGED is saved. When the bot connects again, a gap longer than
  `system.downtime_min_minutes` becomes one owner-log line ("offline for 47 min, from → to, likely reason").
  Several outages in a short time are merged into one notice (at most one per 10 minutes).
* Backups: a consistent SQLite snapshot nightly at `system.backup_hour` (host-local time), keeping the newest
  `system.backup_keep`, plus one catch-up after the machine was off. Owner can back up on demand. Backups stay LOCAL.
  Restoring is deliberately not a button: stop the bot and run the CLI `restore` command (see README).
* Servers: the owner sees when the bot is added to / removed from a server, including while it was offline.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime

import discord
from discord.ext import commands, tasks

from .. import uptime as U

log = logging.getLogger("vrbot.system")
MERGE_WINDOW = 600.0


class System(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.last_backup_result: dict | None = None
        self._backup_lock = asyncio.Lock()
        self._checked_this_connection = False

    @property
    def cfg(self):
        return self.bot.cfg.system

    @property
    def backup_dir(self):
        return self.bot.settings.data_dir / "backups"

    @property
    def db_file(self):
        return self.bot.db.path

    async def cog_load(self):
        self.last_backup_result = await self.bot.db.kv_get("backups:last")
        self.stop_watch.start()

    def cog_unload(self):
        for loop in (self.alive_loop, self.backup_loop, self.flush_loop, self.stop_watch):
            loop.cancel()

    # ---------------------------------------------------------- graceful stop without signals
    @tasks.loop(seconds=2)
    async def stop_watch(self):
        """A process manager without POSIX signals (the Windows launcher) asks for a clean stop by creating
        <data>/stop.request; the bot then shuts down normally, so the stop is recorded as a clean one."""
        req = self.bot.settings.data_dir / "stop.request"
        if req.exists():
            req.unlink(missing_ok=True)
            log.info("stop requested by the process manager")
            await self.bot.close()

    # ---------------------------------------------------------- heartbeat / downtime
    def last_answered(self) -> float | None:
        """Wall-clock time of the last heartbeat Discord acknowledged (None when not connected)."""
        if not self.bot.is_ready() or self.bot.is_closed():
            return None
        lat = self.bot.latency
        if lat != lat or lat == float("inf"):   # NaN/inf: no heartbeat acknowledged yet
            return None
        ka = getattr(getattr(self.bot, "ws", None), "_keep_alive", None)
        last_ack = getattr(ka, "_last_ack", None)
        if isinstance(last_ack, (int, float)):
            return time.time() - (time.perf_counter() - last_ack)
        return time.time()

    async def save_alive(self) -> None:
        t = self.last_answered()
        if t is None:
            return
        prev = await self.bot.db.kv_get("uptime:alive") or 0
        if t > prev:                      # never moves backwards
            await self.bot.db.kv_set("uptime:alive", t)

    async def record_shutdown(self, kind: str) -> None:
        await self.save_alive()
        await self.bot.db.kv_set("uptime:shutdown", {"at": time.time(), "kind": kind})

    @tasks.loop(seconds=30)
    async def alive_loop(self):
        await self.save_alive()

    async def on_connected(self) -> None:
        """Called on READY and RESUMED: report a meaningful gap since the last proven connection."""
        last = await self.bot.db.kv_get("uptime:alive")
        shutdown = await self.bot.db.kv_get("uptime:shutdown")
        now = time.time()
        gap = U.classify(last, shutdown, self.bot.started, now, self.cfg.downtime_min_minutes * 60)
        await self.bot.db.kv_set("uptime:alive", now)
        await self.bot.db.kv_set("uptime:shutdown", None)
        if gap:
            log.warning("downtime detected: %ss (%s)", gap.seconds, gap.reason)
            pending = await self.bot.db.kv_get("uptime:pending") or []
            pending.append(gap.as_dict())
            await self.bot.db.kv_set("uptime:pending", pending)
            await self.bot.db.kv_set("uptime:last_downtime", gap.as_dict())
        await self.flush()

    async def flush(self) -> None:
        """Post queued outages as ONE owner-log notice, at most once per 10 minutes."""
        pending = await self.bot.db.kv_get("uptime:pending") or []
        g = self.bot.guild
        if not pending or not g:
            return
        if time.time() - (await self.bot.db.kv_get("uptime:notified_at") or 0) < MERGE_WINDOW:
            return
        await self.bot.db.kv_set("uptime:pending", [])
        await self.bot.db.kv_set("uptime:notified_at", time.time())
        await self.bot.db.add_event(type="bot_downtime", category="bot", guild_id=g.id, details={"periods": pending},
                                    source="bot")
        if self.cfg.downtime_dm_owner:
            from ..render import downtime_line
            try:
                owner = g.owner or await self.bot.fetch_user(g.owner_id)
                await owner.send(downtime_line({"periods": pending}), allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                log.info("downtime DM not delivered")

    @tasks.loop(minutes=1)
    async def flush_loop(self):
        await self.flush()

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.alive_loop.is_running():
            self.alive_loop.start()
            self.flush_loop.start()
        if not self.backup_loop.is_running() and self.cfg.backups_enabled:
            self.backup_loop.start()
        if not self._checked_this_connection:
            self._checked_this_connection = True
            await self.on_connected()
            await self.check_guild_changes()

    @commands.Cog.listener()
    async def on_resumed(self):
        await self.on_connected()

    @commands.Cog.listener()
    async def on_disconnect(self):
        self._checked_this_connection = False   # the next READY checks again (RESUMED also does)

    # ---------------------------------------------------------- servers added / removed
    async def _guild_event(self, kind: str, guild: discord.Guild | discord.Object, name: str | None,
                           members: int | None, while_offline: bool = False) -> None:
        home = self.bot.guild
        if not home or guild.id == home.id and kind == "bot_guild_join":
            return
        await self.bot.db.add_event(type=kind, category="bot", guild_id=home.id, source="bot",
                                    details={"guild": str(guild.id), "name": name, "members": members,
                                             "while_offline": while_offline})

    async def check_guild_changes(self) -> None:
        known = await self.bot.db.kv_get("guilds:known")
        now = {str(g.id): g.name for g in self.bot.guilds}
        await self.bot.db.kv_set("guilds:known", now)
        if known is None:
            return   # first start: just remember
        for gid, name in now.items():
            if gid not in known:
                g = self.bot.get_guild(int(gid))
                await self._guild_event("bot_guild_join", g, name, getattr(g, "member_count", None), True)
        for gid, name in known.items():
            if gid not in now:
                await self._guild_event("bot_guild_remove", discord.Object(int(gid)), name, None, True)

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        await self.bot.db.kv_set("guilds:known", {str(g.id): g.name for g in self.bot.guilds})
        await self._guild_event("bot_guild_join", guild, guild.name, guild.member_count)

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild):
        await self.bot.db.kv_set("guilds:known", {str(g.id): g.name for g in self.bot.guilds})
        await self._guild_event("bot_guild_remove", guild, guild.name, guild.member_count)

    # ---------------------------------------------------------- backups
    def backups(self):
        return U.list_backups(self.backup_dir)

    def newest_backup_time(self) -> datetime | None:
        b = U.list_backups(self.backup_dir, "auto")
        return U.backup_time(b[0]) if b else None

    async def backup_now(self, reason: str = "scheduled", actor: discord.abc.User | None = None) -> dict:
        async with self._backup_lock:
            now = U.local_now()
            dest = self.backup_dir / U.backup_name(now, "manual" if reason == "manual" else "auto")
            try:
                await asyncio.to_thread(U.clean_leftovers, self.backup_dir)
                size = await asyncio.to_thread(U.snapshot_sqlite, self.db_file, dest)
                removed = await asyncio.to_thread(U.prune, self.backup_dir, self.cfg.backup_keep)
                res = {"ok": True, "at": time.time(), "file": dest.name, "size_kb": round(size / 1024),
                       "reason": reason, "pruned": len(removed)}
            except Exception as e:  # noqa: BLE001
                log.exception("database backup failed")
                res = {"ok": False, "at": time.time(), "error": f"{type(e).__name__}: {e}"[:200], "reason": reason}
            self.last_backup_result = res
            await self.bot.db.kv_set("backups:last", res)
            g = self.bot.guild
            if g and (not res["ok"] or reason == "manual"):   # nightly successes stay quiet; failures never do
                await self.bot.db.add_event(
                    type="db_backup" if res["ok"] else "db_backup_failed", category="bot", guild_id=g.id,
                    actor_id=getattr(actor, "id", None), actor_name=getattr(actor, "display_name", None),
                    actor_confidence="confirmed" if actor else "unknown", details=res, source="bot")
            return res

    @tasks.loop(minutes=10)
    async def backup_loop(self):
        if self.cfg.backups_enabled and U.backup_due(self.newest_backup_time(), U.local_now(), self.cfg.backup_hour):
            await self.backup_now("scheduled")

    def backup_status_lines(self) -> list[str]:
        b = self.backups()
        newest = self.newest_backup_time()
        lines = []
        if not self.cfg.backups_enabled:
            lines.append("**Scheduled backups:** off (`system.backups_enabled`)")
        else:
            nxt = U.next_backup(newest, U.local_now(), self.cfg.backup_hour)
            lines.append(f"**Next backup:** <t:{int(nxt.timestamp())}:R> · daily at {self.cfg.backup_hour:02d}:00 host time · "
                         f"keeps {self.cfg.backup_keep}")
        lines.append(f"**Last backup:** <t:{int(newest.timestamp())}:R> (`{b[0].name}`)" if newest else "**Last backup:** none yet")
        r = self.last_backup_result
        if r:
            lines.append("**Latest result:** " + ("✅ ok" if r.get("ok") else f"❌ {r.get('error')}"))
        lines.append(f"**Stored:** {len(b)} backup(s) in `{self.backup_dir}` on the host (never uploaded)")
        return lines

    async def downtime_status_line(self) -> str:
        d = await self.bot.db.kv_get("uptime:last_downtime")
        up = U.human_duration(int(time.time() - self.bot.started))
        if not d:
            return f"**Online for:** {up} · no downtime recorded"
        return (f"**Online for:** {up} · **last downtime:** {U.human_duration(d['seconds'])} "
                f"(<t:{int(d['from'])}:f> → <t:{int(d['to'])}:t>), {U.REASONS.get(d['reason'], d['reason'])}")


async def setup(bot):
    await bot.add_cog(System(bot))
