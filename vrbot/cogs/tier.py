"""/tier — owner-only trust management. The owner never needs to remember the neutral role names.

Members never see tier terminology: every response here is ephemeral and owner-only, and tier changes are
logged only to the owner logs. Promotions/demotions REPLACE the trust role (exactly one per member).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from ..db import LogQuery, iso
from ..events import bot_reason
from ..trust import DEFAULT_TIER, TIERS, current_tier, normalize_plan, plan_set_tier, tier_role_ids, tiers_held
from ..ui import COLORS

log = logging.getLogger("vrbot.tier")
LABEL = {"tier1": "Level 1 (highest)", "tier2": "Level 2", "tier3": "Level 3 (default)"}
SHORT = {"tier1": "Level 1", "tier2": "Level 2", "tier3": "Level 3"}
SCOPE = {"tier1": "Level 1 + 2 + 3 areas + public", "tier2": "Level 2 + 3 areas + public", "tier3": "Level 3 area + public"}


class TierView(discord.ui.View):
    def __init__(self, cog, member: discord.Member):
        super().__init__(timeout=300)
        self.cog, self.member = cog, member
        for t in TIERS:
            role = cog.bot.guild.get_role(cog.roles().get(t, 0)) if cog.bot.guild else None
            b = discord.ui.Button(label=f"{SHORT[t]} · {role.name if role else '?'}", style=discord.ButtonStyle.primary,
                                  custom_id=f"tier:{t}:{member.id}")
            b.callback = self._make(t)
            self.add_item(b)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        ok = self.cog.owner_like(interaction.user)
        if not ok:
            await interaction.response.send_message("Owner only.", ephemeral=True)
        return ok

    def _make(self, tier: str):
        async def cb(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True, thinking=True)
            res = await self.cog.set_tier(self.member, tier, interaction.user, "owner /tier")
            await interaction.followup.send(embed=self.cog.card(self.member, note=res), ephemeral=True)
        return cb


@app_commands.default_permissions(administrator=True)  # hidden from members' command picker; bot authz enforces
class Tier(commands.GroupCog, group_name="tier", group_description="Owner: trust levels"):
    def __init__(self, bot):
        self.bot = bot
        super().__init__()

    async def cog_load(self):
        menu = app_commands.ContextMenu(name="Manage Trust Level", callback=self.ctx_manage)
        menu.default_permissions = discord.Permissions(administrator=True)  # hidden from non-admins in the client
        self.bot.tree.add_command(menu)

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.reconcile.is_running():
            self.reconcile.start()

    def cog_unload(self):
        self.reconcile.cancel()

    # ---------------------------------------------------------- helpers
    def owner_like(self, user) -> bool:
        g = self.bot.guild
        return bool(g) and self.bot.cfg.privacy.is_owner_like(user.id, g.owner_id)

    def roles(self) -> dict[str, int]:
        return tier_role_ids(self.bot.cfg)

    def card(self, m: discord.Member, note: str | None = None) -> discord.Embed:
        t = current_tier([r.id for r in m.roles], self.roles())
        emb = discord.Embed(title=m.display_name, color=COLORS["BRAND"],
                            description=f"**Current:** {LABEL[t] + ' · ' + self.bot.guild.get_role(self.roles()[t]).name if t else 'no trust level'}\n"
                                        f"**Access:** {SCOPE.get(t, 'public only')}" + (f"\n\n{note}" if note else ""))
        emb.set_footer(text="Private to you · members never see tier names")
        return emb

    async def set_tier(self, member: discord.Member, target: str, actor, why: str, *, allow_owner: bool = False) -> str:
        g = self.bot.guild
        if member.bot:
            return "Bots don't get trust levels."
        if self.bot.cfg.privacy.is_owner_like(member.id, g.owner_id) and not allow_owner:
            return "Owner accounts don't need a trust level."
        roles = self.roles()
        before = current_tier([r.id for r in member.roles], roles)
        add, remove = plan_set_tier([r.id for r in member.roles], target, roles)
        if not add and not remove:
            return f"Already {LABEL[target]}."
        self.bot.guard("tier_change", getattr(actor, "id", None))
        reason = bot_reason(getattr(actor, "display_name", "the bot"), getattr(actor, "id", self.bot.user.id), why)
        if remove:
            await member.remove_roles(*[g.get_role(r) for r in remove], reason=reason)
        if add:
            await member.add_roles(*[g.get_role(r) for r in add], reason=reason)
        live = {int(r) for r in (await self.bot.http.get_member(g.id, member.id))["roles"]}
        held = tiers_held(live, roles)
        ok = held == [target]
        await self.bot.db.add_event(type="tier_change", category="security", guild_id=g.id, target_id=member.id,
                                    target_name=member.display_name, actor_id=getattr(actor, "id", None),
                                    actor_name=getattr(actor, "display_name", "the bot"), actor_confidence="confirmed",
                                    reason=why, details={"from": before, "to": target, "verified": ok, "held_after": held,
                                             "from_name": g.get_role(roles[before]).name if before and g.get_role(roles[before]) else None,
                                             "to_name": g.get_role(roles[target]).name if g.get_role(roles[target]) else None},
                                    source="bot")
        return (f"✅ {LABEL.get(before, 'none')} → **{LABEL[target]}** (verified in Discord)" if ok
                else f"⚠️ Change not reflected as expected: holds {held}")

    # ---------------------------------------------------------- commands (owner / approved owner-equivalent only)
    async def _manage(self, interaction: discord.Interaction, member: discord.Member):
        if not self.owner_like(interaction.user):
            await interaction.response.send_message("You don't have access to that.", ephemeral=True)
            return
        await interaction.response.send_message(embed=self.card(member), view=TierView(self, member), ephemeral=True)

    @app_commands.command(name="manage", description="See and change someone's trust level (private)")
    async def manage(self, interaction: discord.Interaction, member: discord.Member):
        await self._manage(interaction, member)

    async def ctx_manage(self, interaction: discord.Interaction, member: discord.Member):
        await self._manage(interaction, member)

    @app_commands.command(name="legend", description="Which neutral role is which trust level (private)")
    async def legend(self, interaction: discord.Interaction):
        if not self.owner_like(interaction.user):
            await interaction.response.send_message("You don't have access to that.", ephemeral=True)
            return
        g = self.bot.guild
        lines = []
        for t in TIERS:
            r = g.get_role(self.roles()[t])
            lines.append(f"**{LABEL[t]}** → role **{r.name}** · {len(r.members)} member(s) · {SCOPE[t]}")
        none = [m for m in g.members if not m.bot and not self.owner_like(m) and not tiers_held([r.id for r in m.roles], self.roles())]
        lines.append(f"\nNo trust level: {len(none)} (they get Tier 3 automatically)")
        emb = discord.Embed(title="Trust level legend", description="\n".join(lines), color=COLORS["BRAND"])
        emb.set_footer(text="Private to you · role list order is neutral and means nothing; access comes from the floors")
        await interaction.response.send_message(embed=emb, ephemeral=True)

    # ---------------------------------------------------------- default tier + exactly-one rule
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        g = self.bot.guild
        if not g or member.guild.id != g.id or member.bot or self.owner_like(member) or self.bot.safe_mode():
            return
        if not await self.bot.db.kv_get("tier:migrated"):
            return  # enabled after setup (or the default-level step)
        await asyncio.sleep(3)  # let Discord onboarding settle
        m = g.get_member(member.id)
        if m and not tiers_held([r.id for r in m.roles], self.roles()):
            await self.set_tier(m, DEFAULT_TIER, self.bot.user, "default trust level on join")

    async def reconcile_once(self) -> dict:
        """Exactly one tier per normal member: stacked -> keep the highest; none -> Tier 3, unless an owner removed
        the member's tier in the last 24 h (then ask instead of fighting the owner)."""
        g = self.bot.guild
        roles = self.roles()
        since = iso(datetime.now(timezone.utc) - timedelta(hours=24))
        recent = await self.bot.db.query_events(LogQuery(guild_id=g.id, types=["role_remove"], since=since, limit=500))
        owner_removed = {e["target_id"] for e in recent if e.get("role_id") in roles.values()
                         and e.get("actor_id") and self.bot.cfg.privacy.is_owner_like(e["actor_id"], g.owner_id)}
        done = {"normalized": [], "defaulted": [], "skipped_owner_intent": []}
        for m in g.members:
            target, add, remove = normalize_plan([r.id for r in m.roles], roles, is_owner_like=self.owner_like(m), is_bot=m.bot)
            if not target or (not add and not remove):
                continue
            had = tiers_held([r.id for r in m.roles], roles)
            if not had and m.id in owner_removed:
                done["skipped_owner_intent"].append(m.display_name)
                continue
            await self.set_tier(m, target, self.bot.user, "exactly-one trust level" if had else "default trust level")
            (done["normalized"] if had else done["defaulted"]).append(m.display_name)
        if done["skipped_owner_intent"]:
            guardian = self.bot.get_cog("Guardian")
            if guardian:
                await guardian.alert("WARNING", "Member(s) without a trust level after an owner change",
                                     ", ".join(done["skipped_owner_intent"][:20]), key=f"tier-owner:{sorted(done['skipped_owner_intent'])}",
                                     kind="Trust level", recommendation="Use /tier manage to set their level.")
        return done

    @tasks.loop(minutes=30)
    async def reconcile(self):
        if not self.bot.guild or self.bot.safe_mode() or not await self.bot.db.kv_get("tier:migrated"):
            return
        try:
            res = await self.reconcile_once()
            if any(res.values()):
                log.info("tier reconcile: %s", {k: len(v) for k, v in res.items()})
        except Exception:  # noqa: BLE001
            log.exception("tier reconcile failed")


async def setup(bot):
    await bot.add_cog(Tier(bot))
