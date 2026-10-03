"""Join/leave experience: welcome/leave messages, default roles, account-age and raid alerts.

Alerts only — nothing is auto-kicked. All messages are opt-in via config/server.yaml.
"""
from __future__ import annotations

import logging

import discord
from discord.ext import commands

from ..events import bot_reason, utcnow
from ..ui import COLORS

log = logging.getLogger("vrbot.welcome")


class Welcome(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def _fmt(self, template: str, member) -> str:
        return template.format(mention=member.mention, name=member.display_name, server=member.guild.name,
                               count=member.guild.member_count)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        g = self.bot.guild
        if not g or member.guild.id != g.id:
            return
        cfg = self.bot.cfg
        now = utcnow()
        age_days = (now - member.created_at).days
        if not member.bot and age_days < cfg.security.min_account_age_days:
            await self.bot.db.add_event(type="account_age_warning", category="security", guild_id=g.id,
                                        target_id=member.id, target_name=member.display_name, actor_confidence="self",
                                        details={"account_age_days": age_days}, source="bot")
            await self.bot.post_log("account_age_warning", discord.Embed(
                title="New account joined", color=COLORS["WARNING"],
                description=f"{member.mention} — account is **{age_days} day(s)** old."))
            guardian = self.bot.get_cog("Guardian")
            if guardian:
                await guardian.alert("INFO", f"New account joined: {member} ({age_days} day(s) old)", key=f"age:{member.id}",
                                     confidence="self")
        # raid (join-burst) detection lives in the Guardian module
        w = cfg.welcome
        if w.enabled and w.default_roles and not member.bot and not self.bot.safe_mode():
            roles = [r for n in w.default_roles if (r := discord.utils.find(lambda x: x.name.lower() == n.lower(), g.roles))
                     and r < g.me.top_role]
            if roles:
                try:
                    await member.add_roles(*roles, reason=bot_reason("the bot", self.bot.user.id, "default roles on join"))
                except discord.HTTPException:
                    log.warning("could not add default roles", exc_info=True)
        if w.enabled and w.channel:
            ch = self.bot.find_text_channel(w.channel)
            if ch:
                await ch.send(self._fmt(w.message, member), allowed_mentions=discord.AllowedMentions(users=True))
        if w.enabled and w.dm_enabled and not member.bot:
            try:
                await member.send(self._fmt(w.dm_message, member))
            except discord.HTTPException:
                pass

    @commands.Cog.listener()
    async def on_raw_member_remove(self, payload: discord.RawMemberRemoveEvent):
        g = self.bot.guild
        w = self.bot.cfg.welcome
        if not g or payload.guild_id != g.id or not (w.enabled and w.leave_enabled and w.channel):
            return
        ch = self.bot.find_text_channel(w.channel)
        if ch:
            u = payload.user
            await ch.send(w.leave_message.format(name=getattr(u, "display_name", u.name), mention=u.mention,
                                                 server=g.name, count=g.member_count),
                          allowed_mentions=discord.AllowedMentions.none())


async def setup(bot):
    await bot.add_cog(Welcome(bot))
