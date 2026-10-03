"""/access — TEMPORARY authority (never trust). Owner / approved owner-equivalent only.

Temporary Moderator (smallest useful set) and Temporary Administrator roles sit directly below the bot.
Every grant has a mandatory expiry, is persisted (survives restarts; expiries missed during downtime are revoked on
startup), verified over REST, logged to the owner logs and announced by Guardian as INFO/WARNING. Elevation roles
held WITHOUT a bot-recorded grant are removed (Guardian keeps treating manual Administrator as CRITICAL).
The member's trust tier is never touched.
"""
from __future__ import annotations

import logging
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

from ..events import bot_reason
from ..trust import ELEVATIONS, Grant, clamp_duration, due, grant_key
from ..ui import COLORS

log = logging.getLogger("vrbot.access")
KIND_CHOICES = [app_commands.Choice(name=v["label"], value=k) for k, v in ELEVATIONS.items()]


class RevokeView(discord.ui.View):
    def __init__(self, cog, user_id: int, kind: str):
        super().__init__(timeout=None)
        self.cog, self.user_id, self.kind = cog, user_id, kind

    @discord.ui.button(label="Revoke now", emoji="⛔", style=discord.ButtonStyle.danger)
    async def revoke(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.cog.owner_like(interaction.user):
            await interaction.response.send_message("Owner only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        msg = await self.cog.revoke(self.user_id, self.kind, "manual revoke (button)", interaction.user)
        await interaction.followup.send(msg, ephemeral=True)


@app_commands.default_permissions(administrator=True)
class Access(commands.GroupCog, group_name="access", group_description="Owner: temporary moderator/admin access"):
    def __init__(self, bot):
        self.bot = bot
        self.grants: dict[str, dict] = {}
        super().__init__()

    async def cog_load(self):
        self.grants = await self.bot.db.kv_get("access:grants", {}) or {}
        self._sync()
        # known before Guardian's first drift scan (no false "unexpected Administrator role" at startup)
        self.bot.elevation_role_ids = set((await self.role_ids()).values())

    def _sync(self):
        self.bot.temporary_admins = {g["user_id"] for g in self.grants.values() if g["kind"] == "admin"}

    async def _save(self):
        await self.bot.db.kv_set("access:grants", self.grants)
        self._sync()

    async def role_ids(self) -> dict[str, int]:
        return await self.bot.db.kv_get("access:roles", {}) or {}

    def owner_like(self, user) -> bool:
        g = self.bot.guild
        return bool(g) and self.bot.cfg.privacy.is_owner_like(user.id, g.owner_id)

    @commands.Cog.listener()
    async def on_ready(self):
        self.bot.elevation_role_ids = set((await self.role_ids()).values())
        await self.expire_once()  # anything that expired while the bot was down is revoked immediately
        if not self.expiry.is_running():
            self.expiry.start()

    def cog_unload(self):
        self.expiry.cancel()

    # ---------------------------------------------------------- grant / revoke
    async def grant(self, member: discord.Member, kind: str, minutes: int | None, actor, reason: str | None) -> str:
        g = self.bot.guild
        roles = await self.role_ids()
        if kind not in roles:
            return "Temporary access roles are not set up yet — run /setup."
        if member.bot:
            return "Not for bots."
        self.bot.guard("elevation", actor.id)
        mins = clamp_duration(kind, minutes)
        role = g.get_role(roles[kind])
        if role >= g.me.top_role:
            return "The elevation role is not below the bot — cannot manage it safely."
        expires = time.time() + mins * 60
        self.grants[grant_key(member.id, kind)] = Grant(member.id, kind, expires, actor.id, reason).to_dict()
        await self._save()  # persist BEFORE adding the role, so a crash can never leave an unrecorded elevation
        await member.add_roles(role, reason=bot_reason(actor.display_name, actor.id, f"temporary {kind} for {mins} min: {reason or '-'}"))
        live = {int(r) for r in (await self.bot.http.get_member(g.id, member.id))["roles"]}
        ok = role.id in live
        until = f"<t:{int(expires)}:t>"
        await self.bot.db.add_event(type="elevation_grant", category="security", guild_id=g.id, target_id=member.id,
                                    target_name=member.display_name, actor_id=actor.id, actor_name=actor.display_name,
                                    actor_confidence="confirmed", reason=reason,
                                    details={"kind": kind, "minutes": mins, "expires": expires, "verified": ok}, source="bot")
        guardian = self.bot.get_cog("Guardian")
        if guardian:
            await guardian.alert("WARNING" if kind == "admin" else "INFO", f"{ELEVATIONS[kind]['label']} granted to {member.display_name}",
                                 f"Expires {until} ({mins} min). Reason: {reason or '—'}", key=f"elev:{member.id}:{kind}:{int(expires)}",
                                 actor_id=actor.id, actor_name=actor.display_name, kind="Temporary authority",
                                 target=member.display_name, status=f"active until {until}" if ok else "⚠️ not verified",
                                 recommendation="Revoke early with /access revoke if no longer needed.", post=True)
        return f"{'✅' if ok else '⚠️'} **{ELEVATIONS[kind]['label']}** for {member.mention} until {until} ({mins} min). Trust level unchanged."

    async def revoke(self, user_id: int, kind: str, why: str, actor=None) -> str:
        g = self.bot.guild
        roles = await self.role_ids()
        self.grants.pop(grant_key(user_id, kind), None)
        await self._save()
        member = g.get_member(user_id)
        role = g.get_role(roles.get(kind, 0))
        if member and role and role in member.roles:
            # revocation only REMOVES authority: allowed even in Safe Mode
            await member.remove_roles(role, reason=bot_reason(getattr(actor, "display_name", "the bot"),
                                                              getattr(actor, "id", self.bot.user.id), f"temporary {kind} ended: {why}"))
        live = {int(r) for r in (await self.bot.http.get_member(g.id, user_id))["roles"]} if member else set()
        ok = not role or role.id not in live
        await self.bot.db.add_event(type="elevation_revoke", category="security", guild_id=g.id, target_id=user_id,
                                    target_name=getattr(member, "display_name", None), actor_id=getattr(actor, "id", self.bot.user.id),
                                    actor_name=getattr(actor, "display_name", "the bot"), actor_confidence="confirmed",
                                    reason=why, details={"kind": kind, "verified_removed": ok}, source="bot")
        guardian = self.bot.get_cog("Guardian")
        if guardian:
            await guardian.alert("INFO", f"{ELEVATIONS[kind]['label']} ended for {getattr(member, 'display_name', user_id)}",
                                 why, key=f"elev-end:{user_id}:{kind}:{int(time.time())}", kind="Temporary authority",
                                 status="✅ removed (verified)" if ok else "⚠️ still present", post=True)
        return f"{'✅' if ok else '⚠️'} {ELEVATIONS[kind]['label']} removed from <@{user_id}> ({why})."

    async def expire_once(self, now: float | None = None) -> list[str]:
        g = self.bot.guild
        if not g:
            return []
        out = []
        for gr in due(self.grants, now or time.time()):
            out.append(await self.revoke(gr.user_id, gr.kind, "expired"))
        # elevation roles held without an active bot-recorded grant are removed (no silent standing authority)
        roles = await self.role_ids()
        for kind, rid in roles.items():
            role = g.get_role(rid)
            for m in (role.members if role else []):
                if grant_key(m.id, kind) not in self.grants:
                    out.append(await self.revoke(m.id, kind, "held without an approved grant"))
        return out

    @tasks.loop(seconds=20)
    async def expiry(self):
        try:
            await self.expire_once()
        except Exception:  # noqa: BLE001
            log.exception("expiry check failed")

    # ---------------------------------------------------------- commands
    @app_commands.command(name="elevate", description="Temporarily give someone moderator or administrator access")
    @app_commands.choices(kind=KIND_CHOICES)
    @app_commands.describe(minutes="Default: moderator 30, administrator 10. Max: 24 h / 2 h.")
    async def elevate(self, interaction: discord.Interaction, member: discord.Member, kind: str,
                      minutes: app_commands.Range[int, 1, 1440] | None = None, reason: str | None = None):
        if not self.owner_like(interaction.user):
            await interaction.response.send_message("You don't have access to that.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        msg = await self.grant(member, kind, minutes, interaction.user, reason)
        await interaction.followup.send(msg, view=RevokeView(self, member.id, kind), ephemeral=True)

    @app_commands.command(name="revoke", description="End someone's temporary access now")
    async def revoke_cmd(self, interaction: discord.Interaction, member: discord.Member):
        if not self.owner_like(interaction.user):
            await interaction.response.send_message("You don't have access to that.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        kinds = [g["kind"] for g in self.grants.values() if g["user_id"] == member.id] or list(ELEVATIONS)
        msgs = [await self.revoke(member.id, k, "manual revoke", interaction.user) for k in kinds]
        await interaction.followup.send("\n".join(msgs), ephemeral=True)

    @app_commands.command(name="list", description="Active temporary access")
    async def list_(self, interaction: discord.Interaction):
        if not self.owner_like(interaction.user):
            await interaction.response.send_message("You don't have access to that.", ephemeral=True)
            return
        lines = [f"<@{g['user_id']}> · {ELEVATIONS[g['kind']]['label']} · until <t:{int(g['expires'])}:t> (<t:{int(g['expires'])}:R>)"
                 for g in self.grants.values()] or ["None active."]
        await interaction.response.send_message(embed=discord.Embed(title="Temporary access", description="\n".join(lines),
                                                                    color=COLORS["BRAND"]), ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())

    def status_lines(self) -> list[str]:
        return [f"{self.bot.guild.get_member(g['user_id']).display_name if self.bot.guild.get_member(g['user_id']) else g['user_id']}: "
                f"{'Admin' if g['kind'] == 'admin' else 'Moderator'} until <t:{int(g['expires'])}:t>" for g in self.grants.values()]


async def setup(bot):
    await bot.add_cog(Access(bot))
