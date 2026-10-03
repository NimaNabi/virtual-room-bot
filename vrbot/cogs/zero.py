"""/owner_room — Owner area (owner-only private voice) with temporary passes.

Discord lets someone with Move Members drag a member into a voice channel they cannot connect to
(discord.com/community/permissions-on-discord: "Move To … even if they do not have permissions to connect to
the target channel"), so the owner's native drag needs no permission change at all.
/owner_room bring additionally gives a TEMPORARY member pass (view/connect/speak/chat) so the guest also sees the
room's text chat; the pass is removed automatically when they leave Owner area (or never arrive within 10 minutes),
and reconciled after restarts. No Level membership is ever changed.
"""
from __future__ import annotations

import logging
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

from ..bot import require_level
from ..events import bot_reason

log = logging.getLogger("vrbot.zero")
PASS = discord.PermissionOverwrite(view_channel=True, connect=True, speak=True, stream=True, use_voice_activation=True,
                                   send_messages=True, read_message_history=True)
UNUSED_PASS_SECONDS = 600


# only members with Administrator (normally just the owner) see this group in the picker
@app_commands.default_permissions(administrator=True)
class Zero(commands.GroupCog, group_name="owner_room", group_description="Owner-only private voice"):
    def __init__(self, bot):
        self.bot = bot
        self.passes: dict[int, dict] = {}   # member_id -> {"channel": id, "t": epoch, "from": channel id|None}
        super().__init__()

    async def cog_load(self):
        self.passes = {int(k): v for k, v in (await self.bot.db.kv_get("zero:passes", {})).items()}
        self._sync_registry()

    def _sync_registry(self):
        self.bot.temporary_passes = {(v["channel"], mid) for mid, v in self.passes.items()}

    async def _save(self):
        self._sync_registry()
        await self.bot.db.kv_set("zero:passes", {str(k): v for k, v in self.passes.items()})

    def owner_area(self) -> discord.VoiceChannel | None:
        g = self.bot.guild
        p = self.bot.cfg.privacy
        if not g or not p.tiers:
            return None
        key0 = min(p.tiers, key=lambda k: p.tiers[k].rank)
        for cid, tier in p.areas.items():
            ch = g.get_channel(int(cid))
            if tier == key0 and isinstance(ch, discord.VoiceChannel):
                return ch
        return None

    # ---------------------------------------------------------- pass lifecycle
    async def grant(self, member: discord.Member, actor: discord.abc.User) -> discord.VoiceChannel:
        z = self.owner_area()
        if z is None:
            raise app_commands.CheckFailure("Owner area is not configured.")
        self.bot.guard("zero_pass", actor.id)
        await z.set_permissions(member, overwrite=PASS, reason=bot_reason(actor.display_name, actor.id, "Owner area temporary pass"))
        self.passes[member.id] = {"channel": z.id, "t": time.time(),
                                  "from": member.voice.channel.id if member.voice and member.voice.channel else None}
        await self._save()
        await self.bot.db.add_event(type="zero_pass_grant", category="security", guild_id=z.guild.id, target_id=member.id,
                                    target_name=member.display_name, actor_id=actor.id, actor_name=actor.display_name,
                                    actor_confidence="confirmed", channel_id=z.id, channel_name=z.name, source="bot")
        return z

    async def revoke(self, member_id: int, why: str) -> bool:
        info = self.passes.pop(member_id, None)
        await self._save()
        g = self.bot.guild
        z = g.get_channel(info["channel"]) if info else self.owner_area()
        if z is None:
            return False
        target = g.get_member(member_id) or discord.Object(member_id)
        if z.overwrites_for(target).is_empty() and not isinstance(target, discord.Object):
            return bool(info)
        try:
            # revocation is always allowed, even in Safe Mode (it only removes access)
            await self.bot.http.delete_channel_permissions(z.id, member_id, reason=bot_reason("the bot", self.bot.user.id, f"Owner area pass removed: {why}"))
        except discord.NotFound:
            pass
        await self.bot.db.add_event(type="zero_pass_revoke", category="security", guild_id=g.id, target_id=member_id,
                                    target_name=getattr(target, "display_name", None), actor_confidence="confirmed",
                                    actor_name="the bot", channel_id=z.id, channel_name=z.name,
                                    details={"why": why}, source="bot")
        return True

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        info = self.passes.get(member.id)
        if info and before.channel and before.channel.id == info["channel"] and (not after.channel or after.channel.id != info["channel"]):
            await self.revoke(member.id, "left Owner area")

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.sweep.is_running():
            self.sweep.start()

    def cog_unload(self):
        self.sweep.cancel()

    @tasks.loop(minutes=2)
    async def sweep(self):
        """Remove unused/stale passes (never joined, or bot was offline when they left)."""
        g = self.bot.guild
        if not g:
            return
        for mid, info in list(self.passes.items()):
            m = g.get_member(mid)
            inside = m and m.voice and m.voice.channel and m.voice.channel.id == info["channel"]
            if not inside and (info.get("joined") or time.time() - info["t"] > UNUSED_PASS_SECONDS):
                await self.revoke(mid, "left Owner area" if info.get("joined") else "unused pass expired")
            elif inside and not info.get("joined"):
                info["joined"] = True
                await self._save()

    # ---------------------------------------------------------- commands (owner only)
    @app_commands.command(name="bring", description="Bring someone into Owner area with a temporary pass (auto-removed when they leave)")
    @require_level("owner")
    async def bring(self, interaction: discord.Interaction, member: discord.Member):
        if member.bot:
            raise app_commands.CheckFailure("Not for bots.")
        z = await self.grant(member, interaction.user)
        moved = False
        if member.voice and member.voice.channel and member.voice.channel != z:
            try:
                await member.move_to(z, reason=bot_reason(interaction.user.display_name, interaction.user.id, "Owner area bring"))
                moved = True
            except discord.HTTPException:
                pass
        await interaction.response.send_message(
            f"🗝️ {member.mention} has a temporary pass to **{z.name}**" + (" and was moved in." if moved else
            " (not in voice — they can join now; the pass expires in 10 min if unused).") +
            " It is removed automatically when they leave.", ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="remove", description="Take someone out of Owner area and remove their pass")
    @require_level("owner")
    async def remove(self, interaction: discord.Interaction, member: discord.Member):
        z = self.owner_area()
        back = None
        info = self.passes.get(member.id)
        if member.voice and z and member.voice.channel == z:
            back = self.bot.guild.get_channel(info["from"]) if info and info.get("from") else None
            try:
                await member.move_to(back, reason=bot_reason(interaction.user.display_name, interaction.user.id, "Owner area remove"))
            except discord.HTTPException:
                pass
        had = await self.revoke(member.id, "removed by owner")
        await interaction.response.send_message(
            f"🔒 {member.mention} " + (f"moved back to **{back.name}**; " if back else "") + ("pass removed." if had else "had no pass."),
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="status", description="Who is in Owner area and which passes exist")
    @require_level("owner")
    async def status(self, interaction: discord.Interaction):
        z = self.owner_area()
        if z is None:
            raise app_commands.CheckFailure("Owner area is not configured.")
        inside = ", ".join(m.display_name for m in z.members) or "nobody"
        stray = [t.display_name for t, ow in z.overwrites.items() if isinstance(t, discord.Member)
                 and t.id not in self.passes and not t.bot and t.id != z.guild.owner_id]
        lines = [f"**{z.name}** · inside: {inside}",
                 "**Active passes:** " + (", ".join(f"<@{m}>" for m in self.passes) or "none"),
                 "**Unexpected member access:** " + (", ".join(stray) or "none ✅")]
        await interaction.response.send_message("\n".join(lines), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


async def setup(bot):
    await bot.add_cog(Zero(bot))
