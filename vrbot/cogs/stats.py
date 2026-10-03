"""/stats — server history in numbers, plus optional daily/weekly summaries."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from ..bot import require_level
from ..db import LogQuery, iso
from ..guardian_rules import health_score
from ..perms.audit import audit
from ..render import event_line
from ..stats import bar, guardian_counts, per_day, voice_sessions, voice_summary
from ..ui import COLORS, send_pages

log = logging.getLogger("vrbot.stats")
PERIODS = {"today": 1, "week": 7, "month": 30, "quarter": 90}
STRUCT = {"channel_create", "channel_update", "channel_delete", "overwrite_create", "overwrite_update",
          "overwrite_delete", "role_create", "role_update", "role_delete", "guild_update", "bot_add",
          "webhook_create", "webhook_delete", "integration_create"}
PERM = {"overwrite_create", "overwrite_update", "overwrite_delete", "role_update"}
MOD = {"member_ban", "member_kick", "timeout_add", "mod_warn", "member_unban", "voice_disconnect"}


def period_choices():
    return [app_commands.Choice(name=k, value=k) for k in PERIODS]


# hidden from members without Moderate Members in Discord's command picker; bot authz still enforces
@app_commands.default_permissions(moderate_members=True)
class Stats(commands.GroupCog, group_name="stats", group_description="Server history in numbers"):
    def __init__(self, bot):
        self.bot = bot
        super().__init__()

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.summaries.is_running():
            self.summaries.start()

    def cog_unload(self):
        self.summaries.cancel()

    async def _events(self, days: float, **kw) -> list[dict]:
        since = iso(datetime.now(timezone.utc) - timedelta(days=days))
        return await self.bot.db.query_events(LogQuery(guild_id=self.bot.guild.id, since=since, limit=5000, **kw))

    async def coverage_note(self, days: float) -> str:
        first = await self.bot.db.oldest_event_ts(self.bot.guild.id)
        if first and datetime.fromisoformat(first) > datetime.now(timezone.utc) - timedelta(days=days):
            return f"_Data since {first[:10]} (when recording started); older periods are partial._"
        return ""

    # ---------------------------------------------------------- summary builder (also used by /stats overview)
    async def summary_data(self, days: float) -> dict:
        ev = await self._events(days)
        t = lambda types: sum(1 for e in ev if e["type"] in types)  # noqa: E731
        human_voice = [e for e in ev if e["category"] == "voice" and e.get("target_id") != self.bot.user.id]
        vs = voice_summary(voice_sessions(human_voice))
        gc = guardian_counts(ev)
        incidents = [e["reason"] for e in ev if e["type"] == "guardian_alert" and '"severity": "CRITICAL"' in (e.get("details") or "")]
        warnings = [e["reason"] for e in ev if e["type"] == "guardian_alert" and '"severity": "WARNING"' in (e.get("details") or "")]
        major = [event_line(e) for e in ev if e["type"] in STRUCT and e.get("source") != "bot"][:6]
        return {"joins": t({"member_join"}), "leaves": t({"member_leave", "member_kick", "member_ban"}),
                "warns": t({"mod_warn"}), "timeouts": t({"timeout_add"}), "kicks": t({"member_kick"}),
                "bans": t({"member_ban"}), "vdisc": t({"voice_disconnect", "voice_mod_disconnect"}),
                "perm": t(PERM), "struct": t(STRUCT - PERM), "gc": gc, "vs": vs, "incidents": incidents,
                "warnings": warnings, "major": major}

    def meaningful(self, d: dict) -> bool:
        """Daily summary is only posted when something worth an admin's attention happened."""
        return bool(d["joins"] or d["leaves"] or d["warns"] or d["timeouts"] or d["kicks"] or d["bans"]
                    or d["perm"] or d["struct"] or d["gc"]["CRITICAL"] or d["gc"]["WARNING"])

    async def summary_lines(self, days: float, kind: str) -> list[str]:
        d = await self.summary_data(days)
        vs = d["vs"]
        findings = audit(self.bot.model(), self.bot.cfg, self.bot.user.id, list(self.bot.bot_owner_ids()))
        net = d["joins"] - d["leaves"]
        lines = [f"**Members:** +{d['joins']} joined · −{d['leaves']} left · net {net:+d} (now {self.bot.guild.member_count})"]
        if vs["sessions"] or kind != "daily":
            lines.append(f"**Voice:** {vs['sessions']} sessions · {vs['hours']} h · {vs['unique_users']} people"
                         + (f" · most used: {', '.join(c for c, _ in vs['top_channels'][:3])}" if vs["top_channels"] else ""))
        mod = [f"{n} {w}" for n, w in ((d["warns"], "warnings"), (d["timeouts"], "timeouts"), (d["kicks"], "kicks"),
                                          (d["bans"], "bans"), (d["vdisc"], "voice disconnects")) if n]
        lines.append("**Moderation:** " + (", ".join(mod) if mod else "none"))
        lines.append(f"**Permission/config changes:** {d['perm']} permission · {d['struct']} other")
        lines.append(f"**Guardian:** 🔴 {d['gc']['CRITICAL']} · 🟡 {d['gc']['WARNING']} · 🔵 {d['gc']['INFO']}")
        c = sum(f.severity == "CRITICAL" for f in findings)
        w = sum(f.severity == "WARNING" for f in findings)
        lines.append(f"**Open problems now:** 🔴 {c} · 🟡 {w}" + (f" — {findings[0].title}" if findings and findings[0].severity != "INFO" else ""))
        if d["incidents"]:
            lines.append("**Incidents:**")
            lines += [f"• {x}" for x in d["incidents"][:5]]
        elif d["warnings"] and kind != "daily":
            lines.append("**Warnings:**")
            lines += [f"• {x}" for x in d["warnings"][:5]]
        if d["major"] and kind != "daily":
            lines.append("**Major server changes:**")
            lines += [f"• {x}" for x in d["major"]]
        note = await self.coverage_note(days)
        if note:
            lines.append(note)
        return lines

    @app_commands.command(name="overview", description="Summary for a period (members, voice, moderation, changes, guardian)")
    @app_commands.choices(period=period_choices())
    @require_level("mod")
    async def overview(self, interaction: discord.Interaction, period: str = "week"):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await send_pages(interaction, f"📊 {interaction.guild.name} — {period}", await self.summary_lines(PERIODS[period], period))

    @app_commands.command(name="members", description="Joins/leaves per day, growth and Level sizes")
    @app_commands.choices(period=period_choices())
    @require_level("mod")
    async def members(self, interaction: discord.Interaction, period: str = "month"):
        await interaction.response.defer(ephemeral=True, thinking=True)
        ev = await self._events(PERIODS[period], category=None)
        joins = dict(per_day(ev, {"member_join"}))
        leaves = dict(per_day(ev, {"member_leave", "member_kick", "member_ban"}))
        days = sorted(set(joins) | set(leaves))
        mx = max([*joins.values(), *leaves.values(), 1])
        lines = [f"`{d}` +{joins.get(d, 0):<3} {bar(joins.get(d, 0), mx)}  −{leaves.get(d, 0)}" for d in days] or ["No joins/leaves recorded."]
        net = sum(joins.values()) - sum(leaves.values())
        lines.append(f"\n**Net:** {net:+d} · **Now:** {self.bot.guild.member_count} members")
        g = self.bot.guild
        trust_levels = self.bot.trust_level_role_ids()
        lines.append("**Trust levels:** " + ", ".join(f"{k}: {len(g.get_role(r).members)}" for k, r in trust_levels.items() if g.get_role(r))
                     + f" · no role: {sum(1 for m in g.members if len(m.roles) == 1 and not m.bot)}")
        recent = [e for e in ev if e["type"] == "member_join"][:10]
        if recent:
            lines.append("**Recent joins:** " + ", ".join(e["target_name"] or "?" for e in recent))
        note = await self.coverage_note(PERIODS[period])
        if note:
            lines.append(note)
        await send_pages(interaction, f"👥 Members — {period}", lines)

    @app_commands.command(name="voice", description="Voice sessions, hours, busiest rooms and most active people")
    @app_commands.choices(period=period_choices())
    @require_level("mod")
    async def voice(self, interaction: discord.Interaction, period: str = "week"):
        await interaction.response.defer(ephemeral=True, thinking=True)
        ev = await self._events(PERIODS[period], category="voice")
        g = self.bot.guild
        owner_view = self.bot.cfg.privacy.is_owner_like(interaction.user.id, g.owner_id)
        if not owner_view:
            from ..perms.privacy import redact_for_staff, visible_channel_ids
            ev = redact_for_staff(ev, visible_channel_ids(self.bot.model(), interaction.user.id))
        vs = voice_summary(voice_sessions(ev), top=8)
        now = [f"{vc.name}: {', '.join(m.display_name for m in vc.members)}" for vc in g.voice_channels if vc.members
               and (owner_view or vc.permissions_for(interaction.user).view_channel)]
        lines = [f"**Sessions:** {vs['sessions']} · **Total:** {vs['hours']} h · **People:** {vs['unique_users']}",
                 f"**In voice now:** {'; '.join(now) or 'nobody'}", "", "**Busiest rooms:**"]
        mx = max([h for _, h in vs["top_channels"]] or [1])
        lines += [f"`{c[:18]:<18}` {bar(h, mx)} {h} h" for c, h in vs["top_channels"]] or ["—"]
        lines += ["", "**Most active:**"] + ([f"{n}: {h} h" for n, h in vs["top_users"]] or ["—"])
        mod = [e for e in ev if e["type"] in ("voice_disconnect",) or (e["type"] == "voice_move" and e.get("actor_id"))]
        lines.append(f"\n**Moderator voice actions:** {len(mod)} (moves/disconnects by someone else)")
        note = await self.coverage_note(PERIODS[period])
        if note:
            lines.append(note)
        await send_pages(interaction, f"🔊 Voice — {period}", lines)

    @app_commands.command(name="moderation", description="Moderation actions and who performed them")
    @app_commands.choices(period=period_choices())
    @require_level("mod")
    async def moderation(self, interaction: discord.Interaction, period: str = "month"):
        await interaction.response.defer(ephemeral=True, thinking=True)
        ev = [e for e in await self._events(PERIODS[period]) if e["type"] in MOD | {"timeout_remove", "mod_action"}]
        from collections import Counter
        by_type = Counter(e["type"] for e in ev)
        by_actor = Counter(e["actor_name"] or "unknown" for e in ev)
        lines = ["**By action:** " + (", ".join(f"{k} {v}" for k, v in by_type.most_common()) or "none"),
                 "**By moderator:** " + (", ".join(f"{k} {v}" for k, v in by_actor.most_common(8)) or "none"), ""]
        lines += [event_line(e) for e in ev[:15]]
        await send_pages(interaction, f"🔨 Moderation — {period}", lines)

    @app_commands.command(name="changes", description="Server configuration changes (roles, channels, permissions)")
    @app_commands.choices(period=period_choices())
    @require_level("mod")
    async def changes(self, interaction: discord.Interaction, period: str = "week"):
        await interaction.response.defer(ephemeral=True, thinking=True)
        ev = [e for e in await self._events(PERIODS[period], category="structure")]
        lines = [event_line(e) for e in ev[:60]] or ["No structural changes recorded."]
        await send_pages(interaction, f"🧱 Server changes — {period} ({len(ev)})", lines)

    @app_commands.command(name="guardian", description="Guardian alerts over time")
    @app_commands.choices(period=period_choices())
    @require_level("mod")
    async def guardian(self, interaction: discord.Interaction, period: str = "month"):
        ev = await self._events(PERIODS[period], types=["guardian_alert"])
        gc = guardian_counts(ev)
        per = per_day(ev, {"guardian_alert"})
        mx = max([n for _, n in per] or [1])
        lines = [f"🔴 {gc['CRITICAL']} critical · 🟡 {gc['WARNING']} warnings · 🔵 {gc['INFO']} info", ""]
        lines += [f"`{d}` {bar(n, mx)} {n}" for d, n in per]
        await send_pages(interaction, f"🛡️ Guardian — {period}", lines)

    # ---------------------------------------------------------- scheduled summaries (off by default)
    @tasks.loop(minutes=15)
    async def summaries(self):
        cfg = self.bot.cfg.summaries
        g = self.bot.guild
        if not g or not (cfg.daily or cfg.weekly):
            return
        now = datetime.now(timezone.utc)
        if now.hour != cfg.hour_utc:
            return
        for kind, enabled, days in (("daily", cfg.daily, 1), ("weekly", cfg.weekly and now.weekday() == cfg.weekday, 7)):
            if not enabled:
                continue
            key = f"summary:{kind}:{now.date().isoformat()}"
            if await self.bot.db.kv_get(key):
                continue
            ch = self.bot.find_text_channel(cfg.channel) if cfg.channel else None
            if ch is None:
                guardian = self.bot.get_cog("Guardian")
                ch = await guardian.alert_channel() if guardian else None
            if ch is None:
                return
            if kind == "daily" and not self.meaningful(await self.summary_data(1)):
                await self.bot.db.kv_set(key, "skipped: nothing meaningful")
                continue
            lines = await self.summary_lines(days, kind)
            emb = discord.Embed(title=f"{self.bot.guild.name} — {kind.title()} Summary", description="\n".join(lines),
                                color=COLORS["BRAND"], timestamp=now)
            emb.set_footer(text="Summary")
            await ch.send(embed=emb, allowed_mentions=discord.AllowedMentions.none())
            await self.bot.db.kv_set(key, True)


async def setup(bot):
    await bot.add_cog(Stats(bot))
