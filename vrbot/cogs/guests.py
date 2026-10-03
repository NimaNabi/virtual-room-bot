"""/guest — Tier 1 members bring their own friends, mediated by the bot (no Manage Roles for anyone).

Invite Guest → the bot creates a controlled invite (expiring, limited uses, unique) → the friend joins →
invite attribution matches the code → the friend gets the DEFAULT tier only (Tier 3) → if the invite was made
from the sponsor's temporary room, the friend is let into that room → the owner logs record who invited whom.
Give Standard Access: a Tier 1 member may give a member WITHOUT any trust level the default tier — nothing else
(never a higher tier, never a demotion, never another role). Global moderation stays with the owner.
"""
from __future__ import annotations

import logging
import time

import discord
from discord import app_commands
from discord.ext import commands

from ..trust import DEFAULT_TIER, tier_role_ids, tiers_held
from ..ui import COLORS

log = logging.getLogger("vrbot.guests")

MAX_ACTIVE = 3          # active guest invites per sponsor
MAX_PER_DAY = 8         # guest invites created per sponsor per 24 h
MAX_ACCESS_PER_DAY = 10  # Give Standard Access per sponsor per 24 h
DEFAULT_HOURS, MAX_HOURS = 24, 72
DEFAULT_USES, MAX_USES = 1, 5


def can_sponsor(role_ids, tier_roles: dict[str, int], owner_like: bool, allowed=("tier1",)) -> bool:
    """Guest invites: the owner, or members of the levels configured in trust.guest_invite_levels."""
    have = set(role_ids)
    return owner_like or any(tier_roles.get(t) in have for t in allowed)


def standard_access_plan(target_role_ids, tier_roles: dict[str, int], *, is_bot: bool, owner_like: bool) -> tuple[bool, str]:
    """Tier 1 may only grant the DEFAULT tier, and only to someone with no trust level at all."""
    if is_bot:
        return False, "Bots don't need access."
    if owner_like:
        return False, "That account is managed by the owner."
    if tiers_held(target_role_ids, tier_roles):
        return False, "They already have access."
    return True, DEFAULT_TIER


def within_limits(history: list[float], now: float, per_day: int) -> bool:
    return sum(1 for t in history if now - t < 86400) < per_day


@app_commands.default_permissions(create_instant_invite=True)  # only members who may invite see /guest
class Guests(commands.GroupCog, group_name="guest", group_description="Bring a friend into the server"):
    def __init__(self, bot):
        self.bot = bot
        super().__init__()

    async def cog_load(self):
        menu = app_commands.ContextMenu(name="Give Standard Access", callback=self.ctx_access)
        menu.default_permissions = discord.Permissions(create_instant_invite=True)  # hidden from members without it
        self.bot.tree.add_command(menu)

    # ---------------------------------------------------------- helpers
    def roles(self) -> dict[str, int]:
        return tier_role_ids(self.bot.cfg)

    def owner_like(self, user) -> bool:
        g = self.bot.guild
        return bool(g) and self.bot.cfg.privacy.is_owner_like(user.id, g.owner_id)

    def sponsor_ok(self, member) -> bool:
        allowed = [t.replace("trust_level_", "tier") for t in self.bot.cfg.trust.guest_invite_levels]
        return can_sponsor([r.id for r in getattr(member, "roles", [])], self.roles(), self.owner_like(member), allowed)

    async def registry(self) -> dict:
        return await self.bot.db.kv_get("guests:invites", {}) or {}

    async def _prune(self, reg: dict) -> dict:
        """Forget expired / used-up invites (Discord removes them itself)."""
        now = time.time()
        live = {i.code for i in await self.bot.guild.invites()}
        return {c: v for c, v in reg.items() if c in live and v["expires"] > now}

    # ---------------------------------------------------------- invite
    async def create_invite(self, sponsor: discord.Member, *, room: discord.VoiceChannel | None = None,
                            uses: int | None = None, hours: int | None = None) -> tuple[bool, str]:
        if not self.sponsor_ok(sponsor):
            return False, "Bringing new people in isn't enabled for your account — ask the server owner."
        self.bot.guard("guest_invite", sponsor.id)
        g = self.bot.guild
        reg = await self._prune(await self.registry())
        hist = (await self.bot.db.kv_get("guests:created", {}) or {}).get(str(sponsor.id), [])
        now = time.time()
        mine = [c for c, v in reg.items() if v["sponsor"] == sponsor.id]
        if len(mine) >= MAX_ACTIVE and not self.owner_like(sponsor):
            return False, f"You already have {len(mine)} open guest invites (`/guest list`). Cancel one first."
        if not within_limits(hist, now, MAX_PER_DAY) and not self.owner_like(sponsor):
            return False, "That's enough guest invites for today — try again tomorrow."
        uses = max(1, min(uses or DEFAULT_USES, MAX_USES))
        hours = max(1, min(hours or DEFAULT_HOURS, MAX_HOURS))
        target = room
        if target is None:
            wid = await self.bot.db.kv_get("channels:welcome")
            target = g.get_channel(int(wid)) if wid else g.system_channel
        inv = await target.create_invite(max_age=hours * 3600, max_uses=uses, unique=True, temporary=False,
                                         reason=f"[bot] guest invite for {sponsor.display_name} ({sponsor.id})")
        reg[inv.code] = {"sponsor": sponsor.id, "sponsor_name": sponsor.display_name, "room": room.id if room else None,
                         "created": now, "expires": now + hours * 3600, "uses": uses}
        await self.bot.db.kv_set("guests:invites", reg)
        created = await self.bot.db.kv_get("guests:created", {}) or {}
        created[str(sponsor.id)] = [t for t in created.get(str(sponsor.id), []) if now - t < 86400] + [now]
        await self.bot.db.kv_set("guests:created", created)
        inv_cog = self.bot.get_cog("Invites")
        if inv_cog:  # known before anyone can use it → exact attribution
            inv_cog.cache[inv.code] = 0
            inv_cog.meta[inv.code] = {"inviter": sponsor.display_name, "inviter_id": sponsor.id, "channel": target.name}
        await self.bot.db.add_event(type="guest_invite_create", category="membership", guild_id=g.id, actor_id=sponsor.id,
                                    actor_name=sponsor.display_name, actor_confidence="confirmed",
                                    details={"code": inv.code, "uses": uses, "hours": hours, "room": room.name if room else None},
                                    source="bot")
        where = f"straight into **{room.name}**" if room else "into the server"
        return True, (f"🎟️ **Guest invite ready** — brings your friend {where}.\n{inv.url}\n"
                      f"Works {uses}× · expires <t:{int(now + hours * 3600)}:R>. They'll get normal member access.")

    async def on_attributed_join(self, member: discord.Member, code: str | None, confidence: str) -> bool:
        """Called by the Invites cog after attribution. Returns True if this was a guest invite."""
        reg = await self.registry()
        rec = reg.get(code or "")
        if not rec:
            return False
        g = self.bot.guild
        tier = self.bot.get_cog("Tier")
        result = "no change"
        if tier and not tiers_held([r.id for r in member.roles], self.roles()) and not self.owner_like(member):
            result = await tier.set_tier(member, DEFAULT_TIER, g.me, f"guest of {rec['sponsor_name']}")
        room = g.get_channel(rec["room"]) if rec.get("room") else None
        if isinstance(room, discord.VoiceChannel):
            await room.set_permissions(member, view_channel=True, connect=True,
                                       reason=f"[bot] guest of {rec['sponsor_name']} (room access)")
        await self.bot.db.add_event(type="guest_join", category="membership", guild_id=g.id, target_id=member.id,
                                    target_name=member.display_name, actor_id=rec["sponsor"], actor_name=rec["sponsor_name"],
                                    actor_confidence=confidence,
                                    details={"invite_type": "Tier 1 guest invite", "code": code, "assigned": "default Tier 3",
                                             "tier_result": result[:80], "room": room.name if room else None},
                                    source="bot")
        return True

    # ---------------------------------------------------------- standard access
    async def give_access(self, sponsor: discord.Member, target: discord.Member) -> str:
        if not self.sponsor_ok(sponsor):
            return "You don't have access to that."
        ok, why = standard_access_plan([r.id for r in target.roles], self.roles(), is_bot=target.bot,
                                       owner_like=self.owner_like(target))
        if not ok:
            return why
        hist = await self.bot.db.kv_get("guests:access", {}) or {}
        now = time.time()
        mine = [t for t in hist.get(str(sponsor.id), []) if now - t < 86400]
        if len(mine) >= MAX_ACCESS_PER_DAY and not self.owner_like(sponsor):
            return "That's enough for today."
        res = await self.bot.get_cog("Tier").set_tier(target, DEFAULT_TIER, sponsor, f"standard access by {sponsor.display_name}")
        hist[str(sponsor.id)] = mine + [now]
        await self.bot.db.kv_set("guests:access", hist)
        await self.bot.db.add_event(type="standard_access", category="membership", guild_id=self.bot.guild.id,
                                    target_id=target.id, target_name=target.display_name, actor_id=sponsor.id,
                                    actor_name=sponsor.display_name, actor_confidence="confirmed",
                                    details={"assigned": "default Tier 3", "result": res[:80]}, source="bot")
        return f"✅ {target.mention} now has normal member access." if "verified" in res else f"⚠️ {res}"

    async def ctx_access(self, interaction: discord.Interaction, member: discord.Member):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await interaction.followup.send(await self.give_access(interaction.user, member), ephemeral=True,
                                        allowed_mentions=discord.AllowedMentions.none())

    # ---------------------------------------------------------- commands
    @app_commands.command(name="invite", description="Make a one-time invite for a friend (expires automatically)")
    @app_commands.describe(uses="How many people can use it (1–5, default 1)", hours="Valid for (1–72 h, default 24)")
    async def invite(self, interaction: discord.Interaction, uses: app_commands.Range[int, 1, 5] | None = None,
                     hours: app_commands.Range[int, 1, 72] | None = None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        vr = self.bot.get_cog("VoiceRooms")
        room = None
        if vr and interaction.user.voice and interaction.user.voice.channel and vr.rooms.get(interaction.user.voice.channel.id) == interaction.user.id:
            room = interaction.user.voice.channel  # in your own room → the invite leads there
        ok, msg = await self.create_invite(interaction.user, room=room, uses=uses, hours=hours)
        await interaction.followup.send(embed=discord.Embed(description=msg, color=COLORS["OK"] if ok else COLORS["WARNING"]),
                                        ephemeral=True)

    @app_commands.command(name="access", description="Give someone without access normal member access")
    async def access(self, interaction: discord.Interaction, member: discord.Member):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await interaction.followup.send(await self.give_access(interaction.user, member), ephemeral=True,
                                        allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="list", description="Your open guest invites")
    async def list_(self, interaction: discord.Interaction):
        reg = await self._prune(await self.registry())
        await self.bot.db.kv_set("guests:invites", reg)
        mine = [(c, v) for c, v in reg.items() if v["sponsor"] == interaction.user.id]
        lines = [f"`{c}` · {v['uses']}× · expires <t:{int(v['expires'])}:R>" for c, v in mine] or ["No open guest invites."]
        await interaction.response.send_message(embed=discord.Embed(title="Your guest invites", description="\n".join(lines),
                                                                    color=COLORS["BRAND"]), ephemeral=True)

    @app_commands.command(name="cancel", description="Cancel all your open guest invites")
    async def cancel(self, interaction: discord.Interaction):
        reg = await self.registry()
        n = 0
        for inv in await self.bot.guild.invites():
            if reg.get(inv.code, {}).get("sponsor") == interaction.user.id:
                await inv.delete(reason=f"[bot] cancelled by {interaction.user.display_name}")
                reg.pop(inv.code, None)
                n += 1
        await self.bot.db.kv_set("guests:invites", reg)
        await interaction.response.send_message(f"Cancelled {n} invite(s).", ephemeral=True)


async def setup(bot):
    await bot.add_cog(Guests(bot))
