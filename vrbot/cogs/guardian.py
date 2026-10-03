"""/guardian — continuous watchdog: classified alerts, drift detection, bursts, Safe Mode control."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from ..bot import require_level
from ..db import LogQuery, iso
from ..events import JoinRate, utcnow
from ..guardian_rules import (CRITICAL, IMPORTANT_INFO, INFO, KIND, RANK, WARNING, Burst, Ctx, classify, health_score,
                              recommend)
from ..perms import flags as F
from ..perms.audit import audit
from ..ui import COLORS, SEV_ICON, ConfirmView, send_pages, ts_fmt
from .eventlog import _diff_text

log = logging.getLogger("vrbot.guardian")


class OwnerDecisionView(discord.ui.View):
    """Owner choice when auto-heal detects drift that the owner caused (never fight the owner silently)."""

    def __init__(self):
        super().__init__(timeout=None)

    async def _owner(self, interaction) -> bool:
        from ..authz import Level
        if interaction.client.level_of(interaction.user) < Level.OWNER:
            await interaction.response.send_message("Owner only.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Accept as new baseline", emoji="✅", style=discord.ButtonStyle.success, custom_id="ah:accept")
    async def accept(self, interaction, button):
        if not await self._owner(interaction):
            return
        bot = interaction.client
        d = await bot.db.kv_get("autoheal:decision")
        if not d:
            await interaction.response.send_message("Nothing pending.", ephemeral=True)
            return
        acc = set(await bot.db.kv_get("autoheal:accepted", [])) | set(d["keys"])
        await bot.db.kv_set("autoheal:accepted", sorted(acc))
        await bot.db.kv_set("autoheal:decision", None)
        await bot.db.add_event(type="baseline_accepted", category="bot", guild_id=bot.guild.id, actor_id=interaction.user.id,
                               actor_name=interaction.user.display_name, actor_confidence="confirmed",
                               details={"accepted": d["keys"]}, source="bot")
        await interaction.response.send_message("✅ Accepted: auto-heal will not touch these targets. (Ask me to fold them into "
                                                "config/server.yaml to make the baseline permanent.)", ephemeral=True)

    @discord.ui.button(label="Revert change", emoji="↩️", style=discord.ButtonStyle.danger, custom_id="ah:revert")
    async def revert(self, interaction, button):
        if not await self._owner(interaction):
            return
        from ..perms.audit import Change
        from ..repair import apply_changes
        bot = interaction.client
        d = await bot.db.kv_get("autoheal:decision")
        if not d:
            await interaction.response.send_message("Nothing pending.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        res = await apply_changes(bot, [Change.from_dict(c) for c in d["changes"]], source="repair", actor=interaction.user,
                                  summary="owner chose: revert drift to approved baseline")
        await bot.db.kv_set("autoheal:decision", None)
        await interaction.followup.send(f"↩️ Reverted — batch #{res['batch_id']} {res['status']} (verified {res['verified']}).", ephemeral=True)

    @discord.ui.button(label="Ignore 24 h", emoji="⏸️", style=discord.ButtonStyle.secondary, custom_id="ah:ignore")
    async def ignore(self, interaction, button):
        if not await self._owner(interaction):
            return
        import time as _t
        bot = interaction.client
        d = await bot.db.kv_get("autoheal:decision")
        if d:
            snooze = await bot.db.kv_get("autoheal:snooze", {})
            for k in d["keys"]:
                snooze[k] = _t.time() + 86400
            await bot.db.kv_set("autoheal:snooze", snooze)
        await interaction.response.send_message("⏸️ Ignored for 24 hours.", ephemeral=True)


class SafeModeView(discord.ui.View):
    """Owner-only emergency button attached to CRITICAL alerts (persistent custom_id)."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Enable Safe Mode (read-only)", emoji="🛑", style=discord.ButtonStyle.danger,
                       custom_id="guardian:safemode")
    async def enable(self, interaction: discord.Interaction, button: discord.ui.Button):
        from ..authz import Level
        bot = interaction.client
        if bot.level_of(interaction.user) < Level.OWNER:
            await interaction.response.send_message("Owner only.", ephemeral=True)
            return
        (bot.settings.data_dir / "SAFE_MODE").write_text(f"enabled via alert button by {interaction.user.id}\n")
        await bot.refresh_presence()
        await bot.db.add_event(type="safe_mode_on", category="security", guild_id=bot.guild.id, actor_id=interaction.user.id,
                               actor_name=interaction.user.display_name, actor_confidence="confirmed", source="bot")
        await interaction.response.send_message("🛑 Safe Mode ON — monitoring continues, all server changes refused. "
                                                "`/guardian safemode off` to resume.", ephemeral=True)


def _fingerprint(config_path) -> str:
    """Hash of the audit rules + server config: drift comparisons are only valid while both are unchanged."""
    import hashlib
    from pathlib import Path

    from ..perms import audit as audit_mod
    h = hashlib.sha256(Path(audit_mod.__file__).read_bytes())
    try:
        h.update(Path(config_path).read_bytes())
    except OSError:
        pass
    return h.hexdigest()[:16]
SEV_COLOR = {CRITICAL: COLORS["CRITICAL"], WARNING: COLORS["WARNING"], INFO: COLORS["INFO"]}


# hidden from members without Moderate Members in Discord's command picker; bot authz still enforces
@app_commands.default_permissions(moderate_members=True)
class Guardian(commands.GroupCog, group_name="guardian", group_description="Server watchdog and alerts"):
    def __init__(self, bot):
        self.bot = bot
        c = bot.cfg.guardian
        self.mass = Burst(c.mass_action_count, c.mass_action_window_seconds)
        self.denied = Burst(c.failed_command_count, 600)
        s = bot.cfg.security
        self.joins = JoinRate(s.raid_joins, s.raid_window_seconds)
        self.recent: dict[str, datetime] = {}
        super().__init__()

    async def cog_load(self):
        self.bot.add_view(SafeModeView())
        self.bot.add_view(OwnerDecisionView())

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.drift.is_running():
            self.drift.change_interval(minutes=max(2, self.bot.cfg.guardian.drift_check_minutes))
            self.drift.start()

    def cog_unload(self):
        self.drift.cancel()

    # ---------------------------------------------------------- delivery
    async def alert_channel(self) -> discord.TextChannel | None:
        cid = await self.bot.db.kv_get("guardian:alert_channel_id")
        if cid and self.bot.guild:
            ch = self.bot.guild.get_channel(int(cid))
            if isinstance(ch, discord.TextChannel):
                return ch
        ref = self.bot.cfg.guardian.alert_channel or self.bot.cfg.log_channel
        return self.bot.find_text_channel(ref) if ref else None

    async def alert(self, severity: str, title: str, detail: str = "", key: str | None = None,
                    actor_id: int | None = None, actor_name: str | None = None, confidence: str = "confirmed",
                    kind: str | None = None, target: str | None = None, status: str | None = None,
                    recommendation: str | None = None, post: bool | None = None, view=None) -> bool:
        """Store + deliver an alert: WHO did WHAT to WHOM/WHAT, WHEN, severity, confidence, current status,
        recommended action. Returns False when suppressed as a duplicate. `post` forces channel posting
        for important INFO."""
        g = self.bot.guild
        if not g or not self.bot.cfg.guardian.enabled:
            return False
        key = key or f"{severity}:{title}"
        now = utcnow()
        last = self.recent.get(key)
        if last and now - last < timedelta(minutes=self.bot.cfg.guardian.dedupe_minutes):
            return False
        self.recent[key] = now
        await self.bot.db.add_event(type="guardian_alert", category="security", guild_id=g.id, actor_id=actor_id,
                                    actor_name=actor_name, actor_confidence=confidence, reason=title,
                                    details={"severity": severity, "title": title, "detail": detail, "key": key,
                                             "kind": kind, "target": target, "status": status,
                                             "recommendation": recommendation}, source="bot")
        log.info("guardian %s: %s", severity, title)
        emb = discord.Embed(title=f"{SEV_ICON[severity]} {severity}" + (f" — {kind}" if kind else ""),
                            description=f"**{title}**" + (f"\n{detail}" if detail else ""),
                            color=SEV_COLOR[severity], timestamp=now)
        if target:
            emb.add_field(name="Target", value=target[:1024], inline=True)
        if actor_id or actor_name:
            emb.add_field(name="Actor", value=f"{actor_name or ''} {f'<@{actor_id}>' if actor_id else ''}\n*{confidence}*"[:1024], inline=True)
        emb.add_field(name="When", value=discord.utils.format_dt(now, "f"), inline=True)
        if status:
            emb.add_field(name="Status", value=status[:1024], inline=False)
        if recommendation:
            emb.add_field(name="Recommended action", value=recommendation[:1024], inline=False)
        emb.set_footer(text="Guardian")
        view = view or (SafeModeView() if severity == CRITICAL and not self.bot.safe_mode() else None)
        should_post = post if post is not None else RANK[severity] <= RANK[self.bot.cfg.guardian.post_min_severity]
        if should_post:
            ch = await self.alert_channel()
            if ch:
                try:
                    await ch.send(embed=emb, view=view, allowed_mentions=discord.AllowedMentions.none())
                except discord.HTTPException:
                    log.warning("cannot post guardian alert", exc_info=True)
        if severity == CRITICAL and self.bot.cfg.guardian.dm_owner_on_critical:
            owner = g.owner or await self.bot.fetch_user(g.owner_id)
            try:
                await owner.send(embed=emb, view=view)
            except discord.HTTPException:
                pass
        return True

    async def current_status(self, action: str, entry: discord.AuditLogEntry, changes: dict) -> str | None:
        """Re-check the live server: is the problem still there?"""
        g = self.bot.guild
        await asyncio.sleep(1.5)
        try:
            tgt = entry.target
            if action in ("role_create", "role_update"):
                added = (changes.get("permissions") or {}).get("added") or []
                role = g.get_role(getattr(tgt, "id", 0))
                if not role:
                    return "role no longer exists"
                still = [p for p in added if F.has(role.permissions.value, p)]
                return f"🔴 still active ({', '.join(still)})" if still else ("✅ already reverted" if added else None)
            if action.startswith("overwrite_") and getattr(entry.extra, "id", None) == g.id:
                ch = g.get_channel(getattr(tgt, "id", 0))
                if ch:
                    vis = ch.permissions_for(g.default_role).view_channel
                    return "🔴 @everyone can currently view it" if vis else "✅ @everyone cannot view it now"
            if action == "bot_add":
                return "bot is in the server" if g.get_member(getattr(tgt, "id", 0)) else "✅ bot is no longer in the server"
            if action == "member_role_update":
                m = g.get_member(getattr(tgt, "id", 0))
                if m:
                    return "🔴 member currently has Administrator" if m.guild_permissions.administrator else "✅ member has no Administrator now"
        except Exception:  # noqa: BLE001
            log.warning("status check failed", exc_info=True)
        return None

    # ---------------------------------------------------------- audit stream
    @commands.Cog.listener()
    async def on_audit_log_entry_create(self, entry: discord.AuditLogEntry):
        g = self.bot.guild
        if not g or entry.guild.id != g.id:
            return
        action = entry.action.name
        changes = _diff_text(entry)
        actor = getattr(entry.user, "display_name", None) or str(entry.user_id)
        tgt = entry.target
        ctx = Ctx(actor=actor, target=getattr(tgt, "name", None) or getattr(tgt, "display_name", None) or str(getattr(tgt, "id", "?")))
        trust_level_ids = set(self.bot.trust_level_role_ids().values())
        if action.startswith("overwrite_"):
            ex = entry.extra
            ctx.target_is_everyone = getattr(ex, "id", None) == g.id
            ctx.target_is_member = isinstance(ex, (discord.Member, discord.User))
            ctx.target_is_trust_level_role = getattr(ex, "id", None) in trust_level_ids
        elif action.startswith("role_"):
            ctx.target_is_trust_level_role = getattr(tgt, "id", None) in trust_level_ids
            if not getattr(tgt, "name", None):
                nm = changes.get("name") or {}
                ctx.target = nm.get("before") or nm.get("after") or ctx.target
        elif action == "bot_add":
            trusted = {str(x).lower() for x in self.bot.cfg.guardian.trusted_bots}
            ctx.trusted_bot = str(getattr(tgt, "id", "")) in trusted or ctx.target.lower() in trusted
        elif action == "member_role_update":
            added_ids = [r.id for r in (getattr(entry.changes.after, "roles", None) or [])]
            from ..trust import is_approved_elevation
            access = self.bot.get_cog("Access")
            if access and is_approved_elevation(added_ids, entry.user_id, self.bot.user.id,
                                                set(getattr(self.bot, "elevation_role_ids", set())), access.grants,
                                                getattr(tgt, "id", 0)):
                return  # legitimate bot-issued temporary authority: already announced by /access with its expiry
            roles = [g.get_role(r) for r in added_ids]
            ctx.role_grants_admin = any(r and r.permissions.administrator for r in roles)
            ctx.role_grants_dangerous = sorted({p for r in roles if r for p in F.names_of(r.permissions.value) if p in F.DANGEROUS})
        res = classify(action, changes, ctx)
        now = utcnow()
        # bursts by the same actor
        if action in ("ban", "kick", "channel_delete", "role_delete") and entry.user_id != self.bot.user.id:
            if self.mass.add((action, entry.user_id), now):
                await self.alert(CRITICAL, f"Mass {action.replace('_', ' ')}: {self.mass.n}+ in {self.mass.window // 60} min by {actor}",
                                 "Possible compromised account or nuke attempt. Consider `/guardian safemode on` and "
                                 "removing the actor's roles.", key=f"mass:{action}:{entry.user_id}",
                                 actor_id=entry.user_id, actor_name=actor, kind="Possible nuke",
                                 recommendation=recommend(action, CRITICAL, "mass action"))
        if res:
            sev, title = res
            if entry.user_id == self.bot.user.id and sev != CRITICAL:
                return  # our own verified actions are already logged (change batches + audit reason)
            what = []
            for k, v in list(changes.items())[:4]:
                if isinstance(v, dict) and ("added" in v or "removed" in v):
                    s = (" +" + ", ".join(v.get("added") or [])) if v.get("added") else ""
                    s += (" −" + ", ".join(v.get("removed") or [])) if v.get("removed") else ""
                    what.append(f"{k}:{s}")
                elif isinstance(v, dict):
                    what.append(f"{k}: {v.get('before')} → {v.get('after')}")
            detail = ("Change: " + "; ".join(what) if what else "") + (f"\nReason: {entry.reason}" if entry.reason else "")
            target = ctx.target + (f" · for {getattr(entry.extra, 'name', None) or getattr(entry.extra, 'id', '')}"
                                   if action.startswith("overwrite_") and entry.extra is not None else "")
            status = await self.current_status(action, entry, changes) if sev != INFO else None
            await self.alert(sev, title, detail, key=f"audit:{entry.id}", actor_id=entry.user_id, actor_name=actor,
                             kind=KIND.get(action, action.replace("_", " ").title()), target=target, status=status,
                             recommendation=recommend(action, sev, title),
                             post=True if (sev == INFO and action in IMPORTANT_INFO) else None)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        g = self.bot.guild
        if not g or member.guild.id != g.id:
            return
        if member.bot:
            await self.alert(WARNING, f"Bot account {member} joined", "Check `/logs action` for who added it.", key=f"botjoin:{member.id}")
        if self.joins.add(utcnow()):
            await self.alert(CRITICAL, f"Possible raid: {len(self.joins.times)} joins within {int(self.joins.window)}s",
                             "Consider raising the verification level or pausing invites.", key="raid",
                             kind="Raid", confidence="unknown", recommendation=recommend("join", CRITICAL, "raid"))

    async def on_command_denied(self, user, command: str) -> None:
        if self.denied.add(("denied", user.id), utcnow()):
            await self.alert(WARNING, f"Repeated denied admin/mod commands by {user.display_name}",
                             f"{self.denied.n}+ refused attempts in 10 min (last: /{command}).", key=f"denied:{user.id}",
                             actor_id=user.id, actor_name=user.display_name)

    # ---------------------------------------------------------- drift detection
    @tasks.loop(minutes=10)
    async def drift(self):
        g = self.bot.guild
        if not g or not self.bot.cfg.guardian.enabled:
            return
        try:
            findings = [f for f in audit(self.bot.model(), self.bot.cfg, self.bot.user.id, list(self.bot.bot_owner_ids()))
                        if f.severity in (CRITICAL, WARNING)]
            current = {"|".join(map(str, f.key)): (f.severity, f.title) for f in findings}
            prev = await self.bot.db.kv_get("guardian:findings")
            # re-baseline silently when the auditor code or config changed: differences would then reflect
            # our rules, not the server, and must never be reported as "resolved"/"new"
            fp = _fingerprint(self.bot.settings.config_path)
            if await self.bot.db.kv_get("guardian:fingerprint") != fp:
                await self.bot.db.kv_set("guardian:fingerprint", fp)
                prev = None
            await self.bot.db.kv_set("guardian:findings", current)
            await self.bot.db.kv_set("guardian:health", {"ts": iso(utcnow()), "score": health_score(
                audit(self.bot.model(), self.bot.cfg, self.bot.user.id, list(self.bot.bot_owner_ids())))})
            if prev is None:
                return  # first run establishes the baseline silently (no alert flood)
            for k, (sev, title) in current.items():
                if k not in prev:
                    await self.alert(sev, f"New problem: {title}", "Detected by the periodic drift check (who caused it: see "
                                     "`/logs recent category:structure`).", key=f"drift:{k}", confidence="unknown",
                                     kind="Drift", status="🔴 present at the time of the check",
                                     recommendation=recommend("drift", sev, title) or "`/server doctor` → `/baseline repair` shows the dry-run fix.")
            for k, (sev, title) in prev.items():
                if k not in current:
                    await self.alert(INFO, f"No longer detected: {title}",
                                     "The server state changed so this finding no longer applies.", key=f"resolved:{k}",
                                     confidence="unknown", kind="Resolved", status="✅ no longer present", post=True)
        except Exception:  # noqa: BLE001
            log.exception("drift check failed")

    # ---------------------------------------------------------- commands
    @app_commands.command(name="status", description="Guardian overview: health, Safe Mode, recent alerts")
    @require_level("mod")
    async def status(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        g = self.bot.guild
        findings = audit(self.bot.model(), self.bot.cfg, self.bot.user.id, list(self.bot.bot_owner_ids()))
        c = sum(f.severity == CRITICAL for f in findings)
        w = sum(f.severity == WARNING for f in findings)
        i = sum(f.severity == INFO for f in findings)
        since = iso(utcnow() - timedelta(days=1))
        alerts = await self.bot.db.query_events(LogQuery(guild_id=g.id, types=["guardian_alert"], since=since, limit=200))
        by = {s: sum(1 for a in alerts if (a.get("details") or "").find(f'"severity": "{s}"') >= 0) for s in (CRITICAL, WARNING, INFO)}
        ch = await self.alert_channel()
        lines = [f"**Mode:** {'🛑 SAFE MODE (read-only)' if self.bot.safe_mode() else '🟢 active'}",
                 f"**Permission health:** {health_score(findings)}% — 🔴 {c} critical · 🟡 {w} warnings · 🔵 {i} info",
                 f"**Main issue:** {findings[0].title}" if findings else "**Main issue:** none",
                 f"**Alerts (24h):** 🔴 {by[CRITICAL]} · 🟡 {by[WARNING]} · 🔵 {by[INFO]}",
                 f"**Alerts go to:** {ch.mention if ch else 'database only (no alert channel — `/guardian setup-channel`)'}"
                 + (" + owner DM for CRITICAL" if self.bot.cfg.guardian.dm_owner_on_critical else ""),
                 f"**Auto-heal:** {'ON' if self.bot.cfg.autoheal.enabled else 'off'} · **Drift check:** every {self.bot.cfg.guardian.drift_check_minutes} min",
                 "", "**Latest alerts:**"]
        recent = await self.bot.db.query_events(LogQuery(guild_id=g.id, types=["guardian_alert"], limit=8))
        lines += [f"{ts_fmt(a['ts'], 'R')} {a['reason']}" for a in recent] or ["none"]
        await send_pages(interaction, "🛡️ Guardian status", lines, color=COLORS["CRITICAL"] if c else COLORS["OK"])

    @app_commands.command(name="alerts", description="Guardian alert history")
    @app_commands.choices(severity=[app_commands.Choice(name=s, value=s) for s in (CRITICAL, WARNING, INFO)])
    @require_level("mod")
    async def alerts(self, interaction: discord.Interaction, severity: str | None = None, days: app_commands.Range[int, 1, 365] = 7):
        rows = await self.bot.db.query_events(LogQuery(guild_id=self.bot.guild.id, types=["guardian_alert"],
                                                       since=iso(utcnow() - timedelta(days=days)), limit=300,
                                                       text=f'"severity": "{severity}"' if severity else None))
        lines = [f"{ts_fmt(a['ts'], 'f')} {a['reason']}" + (f" — by {a['actor_name']}" if a.get("actor_name") else "") for a in rows] or ["No alerts."]
        await send_pages(interaction, f"Guardian alerts — last {days}d", lines)

    @app_commands.command(name="safemode", description="Emergency READ-ONLY mode: keep monitoring, refuse all server changes")
    @app_commands.choices(state=[app_commands.Choice(name="on", value="on"), app_commands.Choice(name="off", value="off")])
    @require_level("owner")
    async def safemode(self, interaction: discord.Interaction, state: str):
        flag = self.bot.settings.data_dir / "SAFE_MODE"
        if state == "on":
            flag.write_text(f"enabled by {interaction.user} ({interaction.user.id}) at {iso(utcnow())}\n")
        else:
            flag.unlink(missing_ok=True)
        still = self.bot.safe_mode()
        await self.bot.refresh_presence()
        await self.bot.db.add_event(type=f"safe_mode_{state}", category="security", guild_id=self.bot.guild.id,
                                    actor_id=interaction.user.id, actor_name=interaction.user.display_name,
                                    actor_confidence="confirmed", source="bot")
        self.bot._last_safe = still
        note = "" if still == (state == "on") else " (still ON: also set via SAFE_MODE env or config)"
        await interaction.response.send_message(f"Safe Mode is now **{'ON' if still else 'OFF'}**{note}.", ephemeral=True)

    @app_commands.command(name="test", description="Send a test alert to the alert channel")
    @require_level("owner")
    async def test(self, interaction: discord.Interaction):
        ok = await self.alert(INFO, "Guardian test alert", f"Requested by {interaction.user.display_name}.",
                              key=f"test:{utcnow().timestamp()}")
        ch = await self.alert_channel()
        await interaction.response.send_message(
            f"Stored{' and posted to ' + ch.mention if ch and RANK[INFO] <= RANK[self.bot.cfg.guardian.post_min_severity] else ''}."
            if ok else "Suppressed (duplicate).", ephemeral=True)

    @app_commands.command(name="setup-channel", description="Create a private alert channel (owner + bot only)")
    @require_level("owner")
    async def setup_channel(self, interaction: discord.Interaction, name: str = "bot-alerts"):
        g = self.bot.guild
        existing = discord.utils.get(g.text_channels, name=name)

        async def do(i: discord.Interaction):
            self.bot.guard("channel_create", i.user.id)
            ch = existing
            if ch is None:
                ow = {g.default_role: discord.PermissionOverwrite(view_channel=False),
                      g.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True)}
                owner = g.get_member(g.owner_id)
                if owner:
                    ow[owner] = discord.PermissionOverwrite(view_channel=True, read_message_history=True)
                ch = await g.create_text_channel(name, overwrites=ow, topic="Guardian alerts & summaries (private)",
                                                 reason=f"[bot: {i.user.display_name} ({i.user.id})] guardian setup")
            await self.bot.db.kv_set("guardian:alert_channel_id", ch.id)
            await i.followup.send(f"✅ Guardian alerts now go to {ch.mention}.", ephemeral=True)

        what = f"use existing {existing.mention}" if existing else f"create private **#{name}** (hidden from @everyone; visible to you and the bot; admins also see it because Administrator bypasses overwrites)"
        await interaction.response.send_message(f"Guardian will {what}. Proceed?",
                                                view=ConfirmView(interaction.user.id, do, "Set up", danger=False), ephemeral=True)


async def setup(bot):
    await bot.add_cog(Guardian(bot))
