"""/mod — moderation with permission + hierarchy checks, confirmations and case records."""
from __future__ import annotations

from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands

from ..authz import Level, moderation_block
from ..bot import require_level
from ..db import parse_duration
from ..events import bot_reason
from ..ui import COLORS, ConfirmView, send_pages, ts_fmt

MAX_TIMEOUT = timedelta(days=28)


# hidden from members without Moderate Members in Discord's command picker; bot authz still enforces
@app_commands.default_permissions(moderate_members=True)
class Moderation(commands.GroupCog, group_name="mod", group_description="Moderation tools"):
    def __init__(self, bot):
        self.bot = bot
        super().__init__()

    # ---------------------------------------------------------- checks
    def _check(self, interaction: discord.Interaction, target: discord.Member, discord_perm: str) -> None:
        actor = interaction.user
        if self.bot.safe_mode():
            self.bot.guard("check", None)  # raises the SAFE MODE message before any confirmation dialog
        if not getattr(actor.guild_permissions, discord_perm):
            raise app_commands.CheckFailure(f"You need the Discord permission **{discord_perm.replace('_', ' ').title()}**.")
        if not getattr(interaction.guild.me.guild_permissions, discord_perm):
            raise app_commands.CheckFailure(f"The bot lacks **{discord_perm.replace('_', ' ').title()}**.")
        g = interaction.guild
        why = moderation_block(actor_id=actor.id, actor_top=actor.top_role.position, actor_level=self.bot.level_of(actor),
                               target_id=target.id, target_top=target.top_role.position,
                               target_level=self.bot.level_of(target), bot_top=g.me.top_role.position,
                               guild_owner_id=g.owner_id, bot_id=self.bot.user.id)
        if why:
            raise app_commands.CheckFailure(why)

    async def _case(self, interaction, action, target=None, reason=None, duration_s=None, channel=None, extra=None) -> int:
        cid = await self.bot.db.add_case(guild_id=interaction.guild.id, action=action, moderator_id=interaction.user.id,
                                         moderator_name=interaction.user.display_name,
                                         user_id=target.id if target else None,
                                         user_name=getattr(target, "display_name", None) or getattr(target, "name", None),
                                         reason=reason, duration_s=duration_s, channel_id=channel.id if channel else None,
                                         extra=extra)
        # actions without their own gateway event get a log entry here
        if action in ("warn", "purge", "lock", "unlock", "slowmode", "unwarn"):
            await self.bot.db.add_event(type="mod_warn" if action == "warn" else "mod_action", category="moderation",
                                        guild_id=interaction.guild.id, target_id=target.id if target else None,
                                        target_name=getattr(target, "display_name", None),
                                        actor_id=interaction.user.id, actor_name=interaction.user.display_name,
                                        actor_confidence="confirmed", channel_id=channel.id if channel else None,
                                        channel_name=channel.name if channel else None, reason=reason,
                                        details={"action": action, "case": cid, **(extra or {})}, source="bot")
            await self.bot.post_log("mod_action", discord.Embed(
                title=f"Mod: {action}", color=COLORS["WARNING"],
                description=f"**By:** {interaction.user.mention}\n**Target:** {getattr(target, 'mention', getattr(channel, 'mention', '—'))}\n"
                            f"**Reason:** {reason or '—'}\nCase #{cid}"))
        return cid

    def _reason(self, interaction, reason):
        return bot_reason(interaction.user.display_name, interaction.user.id, reason)

    # ---------------------------------------------------------- commands
    @app_commands.command(name="timeout", description="Timeout a member (e.g. 10m, 2h, 1d; max 28d)")
    @require_level("mod")
    async def timeout(self, interaction: discord.Interaction, member: discord.Member, duration: str, reason: str | None = None):
        self._check(interaction, member, "moderate_members")
        try:
            d = parse_duration(duration)
        except ValueError as e:
            raise app_commands.CheckFailure(str(e)) from e
        if d > MAX_TIMEOUT:
            raise app_commands.CheckFailure("Discord allows at most 28 days.")
        self.bot.guard("timeout", interaction.user.id)
        await member.timeout(d, reason=self._reason(interaction, reason))
        cid = await self._case(interaction, "timeout", member, reason, int(d.total_seconds()))
        await interaction.response.send_message(f"⏳ {member.mention} timed out for {duration}. Case #{cid}.", ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="untimeout", description="Remove a timeout")
    @require_level("mod")
    async def untimeout(self, interaction: discord.Interaction, member: discord.Member, reason: str | None = None):
        self._check(interaction, member, "moderate_members")
        self.bot.guard("other", interaction.user.id)
        await member.timeout(None, reason=self._reason(interaction, reason))
        cid = await self._case(interaction, "untimeout", member, reason)
        await interaction.response.send_message(f"✅ Timeout removed for {member.mention}. Case #{cid}.", ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="kick", description="Kick a member (asks for confirmation)")
    @require_level("mod")
    async def kick(self, interaction: discord.Interaction, member: discord.Member, reason: str):
        self._check(interaction, member, "kick_members")

        async def do(i):
            self.bot.guard("kick", interaction.user.id)
            await member.kick(reason=self._reason(interaction, reason))
            cid = await self._case(interaction, "kick", member, reason)
            await i.followup.send(f"👢 Kicked **{member}**. Case #{cid}.", ephemeral=True)

        await interaction.response.send_message(f"Kick **{member}** ({member.id})?\nReason: {reason}",
                                                view=ConfirmView(interaction.user.id, do, "Kick"), ephemeral=True)

    @app_commands.command(name="ban", description="Ban a member or user ID (asks for confirmation)")
    @app_commands.describe(delete_message_days="Delete their messages from the last N days (0-7)")
    @require_level("mod")
    async def ban(self, interaction: discord.Interaction, user: discord.User, reason: str,
                  delete_message_days: app_commands.Range[int, 0, 7] = 0):
        member = interaction.guild.get_member(user.id)
        if member:
            self._check(interaction, member, "ban_members")
        elif not interaction.user.guild_permissions.ban_members:
            raise app_commands.CheckFailure("You need the Discord permission **Ban Members**.")

        async def do(i):
            self.bot.guard("ban", interaction.user.id)
            await interaction.guild.ban(user, reason=self._reason(interaction, reason),
                                        delete_message_seconds=delete_message_days * 86400)
            cid = await self._case(interaction, "ban", user, reason)
            await i.followup.send(f"🔨 Banned **{user}**. Case #{cid}.", ephemeral=True)

        await interaction.response.send_message(f"Ban **{user}** ({user.id})?\nReason: {reason}",
                                                view=ConfirmView(interaction.user.id, do, "Ban"), ephemeral=True)

    @app_commands.command(name="unban", description="Unban a user by ID")
    @require_level("mod")
    async def unban(self, interaction: discord.Interaction, user_id: str, reason: str | None = None):
        if not interaction.user.guild_permissions.ban_members:
            raise app_commands.CheckFailure("You need the Discord permission **Ban Members**.")
        if not user_id.strip().isdigit():
            raise app_commands.CheckFailure("Give the numeric user ID (see `/logs action ban`).")
        user = discord.Object(int(user_id))
        self.bot.guard("other", interaction.user.id)
        await interaction.guild.unban(user, reason=self._reason(interaction, reason))
        cid = await self._case(interaction, "unban", user, reason)
        await interaction.response.send_message(f"✅ Unbanned `{user_id}`. Case #{cid}.", ephemeral=True)

    @app_commands.command(name="warn", description="Record a warning (DMs the member if possible)")
    @require_level("mod")
    async def warn(self, interaction: discord.Interaction, member: discord.Member, reason: str, notify: bool = True):
        self._check(interaction, member, "moderate_members")
        cid = await self._case(interaction, "warn", member, reason)
        dm = ""
        if notify:
            try:
                await member.send(f"⚠️ You received a warning in **{interaction.guild.name}**: {reason}")
                dm = " (DM sent)"
            except discord.HTTPException:
                dm = " (could not DM)"
        n = len([c for c in await self.bot.db.cases_for(interaction.guild.id, member.id, "warn") if c["active"]])
        await interaction.response.send_message(f"⚠️ Warned {member.mention}{dm}. Case #{cid}. Active warnings: {n}.",
                                                ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="warnings", description="Moderation history of a member")
    @require_level("mod")
    async def warnings(self, interaction: discord.Interaction, user: discord.User):
        cases = await self.bot.db.cases_for(interaction.guild.id, user.id)
        lines = [f"#{c['id']} • {ts_fmt(c['ts'], 'd')} • **{c['action']}**{'' if c['active'] else ' (cleared)'} • "
                 f"by {c['moderator_name']} • {c['reason'] or '—'}" for c in cases] or ["No cases."]
        await send_pages(interaction, f"Cases for {user}", lines)

    @app_commands.command(name="unwarn", description="Clear a warning case")
    @require_level("mod")
    async def unwarn(self, interaction: discord.Interaction, case_id: int):
        ok = await self.bot.db.deactivate_case(case_id)
        if ok:
            await self._case(interaction, "unwarn", reason=f"cleared case #{case_id}")
        await interaction.response.send_message("✅ Cleared." if ok else "Case not found.", ephemeral=True)

    @app_commands.command(name="purge", description="Delete recent messages in this channel (confirmation above 20)")
    @require_level("mod")
    async def purge(self, interaction: discord.Interaction, count: app_commands.Range[int, 1, 200],
                    member: discord.Member | None = None):
        ch = interaction.channel
        if not ch.permissions_for(interaction.user).manage_messages:
            raise app_commands.CheckFailure("You need **Manage Messages** here.")

        async def do(i):
            self.bot.guard("other", interaction.user.id)
            deleted = await ch.purge(limit=count, check=(lambda m: m.author.id == member.id) if member else None,
                                     reason=self._reason(interaction, f"purge {count}"))
            await self._case(interaction, "purge", member, f"{len(deleted)} message(s)", channel=ch,
                             extra={"deleted": len(deleted)})
            await i.followup.send(f"🧹 Deleted {len(deleted)} message(s).", ephemeral=True)

        if count > 20:
            await interaction.response.send_message(f"Delete up to {count} messages in {ch.mention}?",
                                                    view=ConfirmView(interaction.user.id, do, "Delete"), ephemeral=True)
        else:
            await interaction.response.defer(ephemeral=True, thinking=True)
            await do(interaction)

    @app_commands.command(name="slowmode", description="Set slow mode (seconds, 0 = off)")
    @require_level("mod")
    async def slowmode(self, interaction: discord.Interaction, seconds: app_commands.Range[int, 0, 21600],
                       channel: discord.TextChannel | None = None):
        ch = channel or interaction.channel
        self.bot.guard("other", interaction.user.id)
        await ch.edit(slowmode_delay=seconds, reason=self._reason(interaction, f"slowmode {seconds}s"))
        await self._case(interaction, "slowmode", reason=f"{seconds}s", channel=ch)
        await interaction.response.send_message(f"🐢 Slow mode in {ch.mention}: {seconds}s.", ephemeral=True)

    @app_commands.command(name="lock", description="Stop @everyone from sending messages in a channel")
    @require_level("mod")
    async def lock(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None, reason: str | None = None):
        ch = channel or interaction.channel
        ow = ch.overwrites_for(interaction.guild.default_role)
        prev = ow.send_messages
        await self.bot.db.kv_set(f"lock:{ch.id}", {"send_messages": prev})
        ow.send_messages = False
        self.bot.guard("other", interaction.user.id)
        await ch.set_permissions(interaction.guild.default_role, overwrite=ow, reason=self._reason(interaction, reason or "lock"))
        await self._case(interaction, "lock", reason=reason, channel=ch, extra={"previous": prev})
        await interaction.response.send_message(f"🔒 {ch.mention} locked. Roles with their own ALLOW (e.g. staff) can still talk.",
                                                ephemeral=True)

    @app_commands.command(name="unlock", description="Undo /mod lock (restores the previous @everyone setting)")
    @require_level("mod")
    async def unlock(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None):
        ch = channel or interaction.channel
        saved = await self.bot.db.kv_get(f"lock:{ch.id}", {"send_messages": None})
        ow = ch.overwrites_for(interaction.guild.default_role)
        ow.send_messages = saved.get("send_messages")
        self.bot.guard("other", interaction.user.id)
        await ch.set_permissions(interaction.guild.default_role, overwrite=None if ow.is_empty() else ow,
                                 reason=self._reason(interaction, "unlock"))
        await self._case(interaction, "unlock", channel=ch)
        await interaction.response.send_message(f"🔓 {ch.mention} unlocked.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(Moderation(bot))
