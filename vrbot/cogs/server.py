"""/server status and /server doctor."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands

from ..bot import require_level
from ..db import LogQuery, iso
from ..perms.audit import SEV_ORDER, audit
from ..render import event_line
from ..ui import COLORS, send_pages
from .permissions import render_findings


# hidden from members without Moderate Members in Discord's command picker; bot authz still enforces
@app_commands.default_permissions(moderate_members=True)
class Server(commands.GroupCog, group_name="server", group_description="Server overview and health"):
    def __init__(self, bot):
        self.bot = bot
        super().__init__()

    async def status_lines(self, viewer: discord.abc.User | None = None) -> list[str]:
        g = self.bot.guild
        owner_view = viewer is None or self.bot.cfg.privacy.is_owner_like(viewer.id, g.owner_id)
        since = iso(datetime.now(timezone.utc) - timedelta(days=7))
        counts = await self.bot.db.count_events(since, g.id)
        humans = [m for m in g.members if not m.bot]
        lines = [f"**Members:** {len(humans)} humans, {g.member_count - len(humans)} bots"]
        try:
            full = await self.bot.fetch_guild(g.id, with_counts=True)
            if full.approximate_presence_count is not None:
                lines.append(f"**Online (approx.):** {full.approximate_presence_count}")
        except discord.HTTPException:
            pass
        # privacy: a non-owner only sees occupants of rooms they can see (never Owner area / higher floors)
        viewer_m = g.get_member(viewer.id) if viewer else None
        voice = [(vc, [m.display_name for m in vc.members]) for vc in g.voice_channels + g.stage_channels if vc.members
                 and (owner_view or (viewer_m and vc.permissions_for(viewer_m).view_channel))]
        if voice:
            lines.append("**In voice now:** " + "; ".join(f"🔊 {vc.name}: {', '.join(ms)}" for vc, ms in voice))
        else:
            lines.append("**In voice now:** nobody")
        lines.append(f"**Last 7 days:** {counts.get('member_join', 0)} joins, {counts.get('member_leave', 0)} leaves, "
                     f"{counts.get('member_kick', 0)} kicks, {counts.get('member_ban', 0)} bans, "
                     f"{counts.get('timeout_add', 0)} timeouts, {counts.get('voice_join', 0)} voice joins")
        findings = audit(self.bot.model(), self.bot.cfg, self.bot.user.id, list(self.bot.bot_owner_ids()))
        c = sum(f.severity == "CRITICAL" for f in findings)
        w = sum(f.severity == "WARNING" for f in findings)
        lines.append(f"**Permission health:** {'🔴' if c else '🟡' if w else '🟢'} {c} critical, {w} warnings (`/server doctor`)")
        if owner_view:
            from ..trust import TIERS, tier_role_ids
            tr = tier_role_ids(self.bot.cfg)
            counts = {t: len(g.get_role(tr[t]).members) if t in tr and g.get_role(tr[t]) else 0 for t in TIERS}
            untiered = sum(1 for m in g.members if not m.bot and not self.bot.cfg.privacy.is_owner_like(m.id, g.owner_id)
                           and not any(r.id in tr.values() for r in m.roles))
            lines.append(f"**Trust levels:** Tier 1: {counts['tier1']} · Tier 2: {counts['tier2']} · Tier 3: {counts['tier3']}"
                         + (f" · without a level: {untiered}" if untiered else ""))
            access = self.bot.get_cog("Access")
            elev = access.status_lines() if access else []
            lines.append("**Temporary access:** " + ("; ".join(elev) if elev else "none"))
            from ..perms.privacy import privacy_audit
            pv = privacy_audit(self.bot.model(), self.bot.cfg, self.bot.user.id)
            bypass = [x.title.split(": ", 1)[1] for x in pv if x.code == "privacy_admin_bypass"]
            leaks = [x for x in pv if x.code in ("privacy_leak", "privacy_tier_leak") and not any(b in x.title for b in bypass)]
            social = [x for x in pv if x.code.startswith("social_")]
            lines.append(f"**Privacy:** {'🟢 floors sealed' if not leaks else f'🔴 {len(leaks)} leak(s)'}"
                         + (f" · ⚠️ Administrator bypass: {', '.join(bypass)}" if bypass else "")
                         + (f" · 🟡 {len(social)} social issue(s)" if social else " · social 🟢"))
            day = iso(datetime.now(timezone.utc) - timedelta(days=1))
            alerts = await self.bot.db.query_events(LogQuery(guild_id=g.id, types=["guardian_alert"], since=day, limit=300))
            crit = sum('"severity": "CRITICAL"' in (a.get("details") or "") for a in alerts)
            pending = await self.bot.db.kv_get("autoheal:decision")
            lines.append(f"**Guardian (24h):** {len(alerts)} alert(s), {crit} critical · "
                         f"**Auto-heal:** {'ON' if self.bot.cfg.autoheal.enabled else 'off'}"
                         + (" · ⏸️ decision pending in the alerts channel" if pending else "")
                         + f" · **Mode:** {'🛑 SAFE MODE' if self.bot.safe_mode() else 'active'}")
        mods = await self.bot.db.query_events(LogQuery(guild_id=g.id, category="moderation", since=since, limit=5))
        if mods:
            lines.append("\n**Recent moderation:**")
            lines += [f"• {event_line(e)}" for e in mods]
        joins = await self.bot.db.query_events(LogQuery(guild_id=g.id, types=["member_join"], since=since, limit=5))
        if joins:
            lines.append("**Recent joins:** " + ", ".join(e["target_name"] or "?" for e in joins))
        lines.append("")
        music = self.bot.get_cog("Music")
        lines.append(f"**Bot:** up {timedelta(seconds=int(time.time() - self.bot.started))}, "
                     f"latency {round(self.bot.latency * 1000)} ms")
        db = await self.bot.db.stats()
        lines.append(f"**Database:** {'ok' if await self.bot.db.ping() else 'ERROR'} — {db.get('events')} events, "
                     f"{db.get('snapshots')} snapshots, {db.get('size_mb', '?')} MB")
        lines.append(f"**Music:** {music.status_text() if music else 'module not loaded'}")
        ai = self.bot.get_cog("AI")
        lines.append(f"**AI:** {ai.status_text() if ai else 'module not loaded'}")
        bad = {k: v for k, v in self.bot.module_status.items() if v not in ("loaded", "ok")}
        lines.append("**Modules:** " + ("all loaded" if not bad else "; ".join(f"{k}: {v}" for k, v in bad.items())))
        return lines

    @app_commands.command(name="status", description="Server overview: members, voice, activity, health")
    @require_level("mod")
    async def status(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await send_pages(interaction, f"{self.bot.guild.name} — status", await self.status_lines(interaction.user))

    @app_commands.command(name="doctor", description="Complete health check of permissions, roles, bot and Trust levels")
    @require_level("mod")
    async def doctor(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        findings = audit(self.bot.model(), self.bot.cfg, self.bot.user.id, list(self.bot.bot_owner_ids()))
        extra = []
        g = self.bot.guild
        if not self.bot.cfg.baseline.trust_levels:
            extra.append("🟡 **WARNING** No Trust levels configured yet — edit `config/server.yaml` (see docs).")
        if self.bot.cfg.log_channel and not self.bot.find_text_channel(self.bot.cfg.log_channel):
            extra.append(f"🟡 **WARNING** log_channel `{self.bot.cfg.log_channel}` not found.")
        bad = {k: v for k, v in self.bot.module_status.items() if v not in ("loaded", "ok")}
        for k, v in bad.items():
            extra.append(f"🟡 **WARNING** module {k}: {v}")
        if g.mfa_level == discord.MFALevel.disabled:
            extra.append("🔵 INFO 2FA requirement for moderation is off (Server Settings → Safety Setup).")
        worst = min((SEV_ORDER[f.severity] for f in findings), default=3)
        color = [COLORS["CRITICAL"], COLORS["WARNING"], COLORS["INFO"], COLORS["OK"]][worst]
        lines = render_findings(findings, 25) + ([""] + extra if extra else [])
        await send_pages(interaction, f"Server doctor — {len(findings)} finding(s)", lines, color=color)


async def setup(bot):
    await bot.add_cog(Server(bot))
