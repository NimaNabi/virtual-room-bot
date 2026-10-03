"""Owner-only presence log (online / idle / dnd / offline) — OFF unless PRESENCE_INTENT=true.

Requires the privileged Presence Intent (Developer Portal → Bot). Debounced: a status is only recorded once it
has held for SETTLE seconds, so quick flaps (mobile reconnects, idle blips) never reach the log. Goes to the event
DB (category "presence", owner-only, never the voice activity channel); /logs and the daily summary read it. Activities/games and
custom status text are NOT stored.
"""
from __future__ import annotations

import asyncio
import time

import discord
from discord.ext import commands

SETTLE = 120
LABEL = {"online": "came online", "idle": "went idle", "dnd": "set Do Not Disturb", "offline": "went offline"}


def settle(pending: dict, last: dict, now: float, settle_s: int = SETTLE) -> list[tuple[int, str]]:
    """Statuses that held for settle_s and differ from the last recorded one → to record (pure, tested)."""
    out = []
    for uid, (status, since) in list(pending.items()):
        if now - since >= settle_s:
            pending.pop(uid)
            if last.get(uid) != status:
                last[uid] = status
                out.append((uid, status))
    return out


class Presence(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.pending: dict[int, tuple[str, float]] = {}
        self.last: dict[int, str] = {}
        self.task: asyncio.Task | None = None

    async def cog_load(self):
        if self.bot.settings.presence_intent:
            self.task = asyncio.create_task(self.loop())

    def cog_unload(self):
        if self.task:
            self.task.cancel()

    @commands.Cog.listener()
    async def on_ready(self):
        g = self.bot.guild
        if g and self.bot.settings.presence_intent:
            self.last = {m.id: str(m.status) for m in g.members if not m.bot}  # baseline: no burst of "came online"

    @commands.Cog.listener()
    async def on_presence_update(self, before: discord.Member, after: discord.Member):
        g = self.bot.guild
        if not g or after.guild.id != g.id or after.bot or str(before.status) == str(after.status):
            return
        self.pending[after.id] = (str(after.status), time.time())

    async def loop(self):
        await self.bot.wait_until_ready()
        while True:
            await asyncio.sleep(30)
            g = self.bot.guild
            for uid, status in settle(self.pending, self.last, time.time()):
                m = g.get_member(uid) if g else None
                await self.bot.db.add_event(type=f"presence_{status}", category="presence", guild_id=g.id, target_id=uid,
                                            target_name=getattr(m, "display_name", None), actor_confidence="confirmed",
                                            details={"status": status, "text": LABEL.get(status, status)}, source="gateway")


async def setup(bot):
    await bot.add_cog(Presence(bot))
