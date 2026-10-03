"""Owner-only invite intelligence: which invite brought each new member.

Method (same as common invite trackers): cache invite use-counts, and on each join diff them. One increased
invite = exact ("confirmed"); several at once (simultaneous joins) = "ambiguous"; none (vanity URL, expired
one-use invite, Discord discovery) = "unknown". Results go to the event log (→ the server activity channel, /logs), never to
public channels — no popularity contest.
"""
from __future__ import annotations

import logging

import discord
from discord.ext import commands

log = logging.getLogger("vrbot.invites")


def diff_invites(before: dict[str, int], after: dict[str, int]) -> list[str]:
    """Codes whose use-count increased (new codes count if they already have uses)."""
    return [code for code, uses in after.items() if uses > before.get(code, 0)]


class Invites(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.cache: dict[str, int] = {}
        self.meta: dict[str, dict] = {}

    async def _snapshot(self) -> dict[str, int]:
        g = self.bot.guild
        invs = await g.invites()
        for i in invs:
            self.meta[i.code] = {"inviter": getattr(i.inviter, "display_name", None), "inviter_id": getattr(i.inviter, "id", None),
                                 "channel": getattr(i.channel, "name", None)}
        return {i.code: i.uses or 0 for i in invs}

    @commands.Cog.listener()
    async def on_ready(self):
        if self.bot.guild:
            try:
                self.cache = await self._snapshot()
            except discord.HTTPException:
                log.warning("cannot read invites")

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        g = self.bot.guild
        if not g or member.guild.id != g.id:
            return
        try:
            after = await self._snapshot()
        except discord.HTTPException:
            return
        used = diff_invites(self.cache, after)
        gone = [c for c in self.cache if c not in after]  # single-use invites vanish when used
        self.cache = after
        if len(used) == 1:
            code, conf = used[0], "confirmed"
        elif len(used) > 1:
            code, conf = ", ".join(used), "ambiguous"
        elif len(gone) == 1:
            code, conf = gone[0], "likely"
        else:
            code, conf = None, "unknown"
        info = self.meta.get(code or "", {})
        guests = self.bot.get_cog("Guests")
        if guests and conf != "ambiguous" and code:
            try:
                if await guests.on_attributed_join(member, code, conf):
                    return  # recorded as guest_join (sponsor, invite type, default tier)
            except Exception:  # noqa: BLE001
                log.exception("guest join handling failed")
        await self.bot.db.add_event(type="invite_used", category="membership", guild_id=g.id, target_id=member.id,
                                    target_name=member.display_name, actor_id=info.get("inviter_id"),
                                    actor_name=info.get("inviter"), actor_confidence=conf,
                                    details={"code": code, "channel": info.get("channel"),
                                             "note": "vanity URL, discovery or expired invite" if code is None else None},
                                    source="bot")


async def setup(bot):
    await bot.add_cog(Invites(bot))
