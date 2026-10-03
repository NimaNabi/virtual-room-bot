"""Owner-only raw activity logs: every stored event is streamed (batched) to private channels.

voice activity channel  — all voice activity (joins/leaves/moves, mute/deafen, streaming, camera, AFK, temp rooms, sessions)
server activity channel — membership, moderation, roles, channels, permissions, invites, bots, repairs, Safe Mode
Guardian alerts keep their own low-volume alerts channel. Structured history stays searchable via /logs.
Channel IDs live in kv (set by the setup wizard); visibility is owner + bot only.
"""
from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import commands, tasks

from ..render import owner_log_line

log = logging.getLogger("vrbot.ownerlogs")
SKIP = {"guardian_alert", "ai_query", "bot_started", "message_delete", "message_edit", "message_bulk_delete",
        "guardian_correction"}


class OwnerLogs(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.queues: dict[str, list[str]] = {"voice": [], "server": []}
        self.sent = 0

    async def cog_load(self):
        self.bot.db.event_hooks.append(self.on_event)

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.flush.is_running():
            self.flush.start()

    def cog_unload(self):
        self.flush.cancel()
        if self.on_event in self.bot.db.event_hooks:
            self.bot.db.event_hooks.remove(self.on_event)

    def on_event(self, row: dict) -> None:
        g = self.bot.guild
        if not g or row.get("guild_id") not in (g.id, None) or row["type"] in SKIP:
            return
        if row["category"] == "presence":
            return  # presence stays in the DB (/logs, daily summary) — never streamed into a channel
        if row.get("target_id") == getattr(self.bot.user, "id", None) and row["category"] == "voice":
            return  # the bot's own voice sessions (music/self-tests) are not member activity
        dest = "voice" if row["category"] == "voice" else "server"
        self.queues[dest].append(owner_log_line(row))

    async def channel(self, kind: str) -> discord.TextChannel | None:
        cid = await self.bot.db.kv_get(f"ownerlogs:{kind}")
        ch = self.bot.guild.get_channel(int(cid)) if cid and self.bot.guild else None
        return ch if isinstance(ch, discord.TextChannel) else None

    @tasks.loop(seconds=4)
    async def flush(self):
        if not self.bot.is_ready() or not self.bot.guild:
            return
        for kind, q in self.queues.items():
            if not q:
                continue
            ch = await self.channel(kind)
            if ch is None:
                q.clear()  # not configured: DB still has everything
                continue
            batch, size = [], 0
            while q and size + len(q[0]) + 1 < 1900:
                line = q.pop(0)
                batch.append(line)
                size += len(line) + 1
            if not batch:  # a single very long line
                batch = [q.pop(0)[:1900]]
            try:
                await ch.send("\n".join(batch), allowed_mentions=discord.AllowedMentions.none(), silent=True)
                self.sent += len(batch)
            except discord.HTTPException:
                log.warning("owner log send failed", exc_info=True)
                await asyncio.sleep(5)


async def setup(bot):
    await bot.add_cog(OwnerLogs(bot))
