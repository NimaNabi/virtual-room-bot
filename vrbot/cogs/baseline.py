"""/baseline — Level baseline, safe repair, rollback and optional auto-heal."""
from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

from ..authz import Level
from ..bot import require_level
from ..events import bot_reason
from ..perms import flags as F
from ..perms.audit import audit, plan_repair
from ..repair import apply_changes, rollback_batch
from ..ui import COLORS, ConfirmView, send_pages, ts_fmt
from .permissions import render_findings, render_plan

log = logging.getLogger("vrbot.trust_level")
# codes auto-heal may touch: simple drift only, never structural/admin changes
AUTOHEAL_CODES = {"trust_level_missing_perm", "channel_access", "everyone_missing_perm"}  # never admin/structure


# owner infrastructure: hidden from members in Discord's command picker (bot authz still enforces)
@app_commands.default_permissions(administrator=True)
class Baseline(commands.GroupCog, group_name="baseline", group_description="Owner: access groups and baseline"):
    def __init__(self, bot):
        self.bot = bot
        super().__init__()
        self.autoheal.change_interval(minutes=max(10, bot.cfg.autoheal.interval_minutes))

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.autoheal.is_running():
            self.autoheal.start()

    def cog_unload(self):
        self.autoheal.cancel()

    def repair_level(self) -> Level:
        return Level.parse(self.bot.cfg.access.repair_min_level)

    async def _trust_level_ac(self, interaction, current: str):
        keys = list(self.bot.cfg.baseline.trust_levels) + ["default"]
        return [app_commands.Choice(name=k, value=k) for k in keys if current.lower() in k]

    @app_commands.command(name="list", description="Show the Level design and current members")
    @require_level("owner")
    async def list_(self, interaction: discord.Interaction):
        g = self.bot.guild
        ids = self.bot.trust_level_role_ids()
        lines = []
        for key, c in sorted(self.bot.cfg.baseline.trust_levels.items(), key=lambda kv: kv[1].rank):
            role = g.get_role(ids[key]) if key in ids else None
            lines.append(f"**{key}** (rank {c.rank}) → {role.mention if role else f'⚠️ role `{c.role}` missing'}")
            if c.description:
                lines.append(f"  {c.description}")
            if role:
                names = [m.display_name for m in role.members]
                lines.append(f"  👥 {len(names)}: {', '.join(names[:30]) or '—'}")
            if c.require:
                lines.append(f"  requires: {', '.join(F.pretty(p) for p in c.require)}")
        lines.append(f"\nChannel rules: {len(self.bot.cfg.baseline.channels)} — see `config/server.yaml`.")
        await send_pages(interaction, "Permission Trust levels", lines)

    @app_commands.command(name="check", description="Compare the live server with the Level baseline")
    @app_commands.autocomplete(trust_level=_trust_level_ac)
    @require_level("mod")
    async def check(self, interaction: discord.Interaction, trust_level: str | None = None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        findings = [f for f in audit(self.bot.model(), self.bot.cfg, self.bot.user.id, list(self.bot.bot_owner_ids()))
                    if f.trust_level is not None and (trust_level is None or f.trust_level == trust_level)]
        await send_pages(interaction, f"Level drift{f' — {trust_level}' if trust_level else ''}: {len(findings)}",
                         render_findings(findings), color=COLORS["WARNING"] if findings else COLORS["OK"])

    @app_commands.command(name="repair", description="Show proposed fixes (dry run), then apply after confirmation")
    @app_commands.autocomplete(trust_level=_trust_level_ac)
    async def repair(self, interaction: discord.Interaction, trust_level: str | None = None):
        if self.bot.level_of(interaction.user) < self.repair_level():
            raise app_commands.CheckFailure(f"Repairs need **{self.repair_level().name.title()}** access.")
        await interaction.response.defer(ephemeral=True, thinking=True)
        plan = plan_repair(self.bot.model(), self.bot.cfg, self.bot.user.id, scope=trust_level)
        lines = render_plan(plan)
        if not plan.changes:
            await send_pages(interaction, "Level repair", lines, color=COLORS["OK"])
            return
        if plan.blocked:
            await send_pages(interaction, "Level repair — blocked", lines, color=COLORS["CRITICAL"])
            return
        lines.append("\nA snapshot is saved first; every change is verified afterwards and can be undone with `/baseline rollback`.")

        async def do_apply(i: discord.Interaction):
            fresh = plan_repair(self.bot.model(), self.bot.cfg, self.bot.user.id, scope=trust_level)
            if [c.to_dict() for c in fresh.changes] != [c.to_dict() for c in plan.changes]:
                await i.followup.send("Server state changed since the dry run. Run `/baseline repair` again.", ephemeral=True)
                return
            res = await apply_changes(self.bot, plan.changes, source="repair", actor=i.user,
                                      summary=f"trust_level repair ({trust_level or 'all'}): {len(plan.changes)} change(s)")
            await i.followup.send(embed=_result_embed(res), ephemeral=True)

        view = ConfirmView(interaction.user.id, do_apply, confirm_label=f"Apply {len(plan.changes)} change(s)")
        await send_pages(interaction, "Level repair — dry run", lines, color=COLORS["WARNING"])
        await interaction.followup.send("Apply these changes?", view=view, ephemeral=True)

    @app_commands.command(name="rollback", description="Undo a previous repair/restore batch")
    async def rollback(self, interaction: discord.Interaction, batch_id: int):
        if self.bot.level_of(interaction.user) < self.repair_level():
            raise app_commands.CheckFailure(f"Rollback needs **{self.repair_level().name.title()}** access.")
        b = await self.bot.db.get_batch(batch_id)
        if not b:
            raise app_commands.CheckFailure(f"Batch #{batch_id} not found.")
        from ..perms.audit import Change
        lines = [f"Batch #{batch_id} ({b['source']}, {b['status']}, {ts_fmt(b['ts'])}) — reverting:"]
        lines += [f"• {Change.from_dict(d).describe()}" for d in b["changes"]]

        async def do(i):
            res = await rollback_batch(self.bot, batch_id, i.user)
            if "error" in res:
                await i.followup.send(f"⚠️ {res['error']}" + (f"\nChanged since: {res.get('drifted')}" if res.get("drifted") else ""), ephemeral=True)
            else:
                await i.followup.send(embed=_result_embed(res, "Rollback"), ephemeral=True)

        await send_pages(interaction, "Rollback preview", lines)
        await interaction.followup.send("Roll back?", view=ConfirmView(interaction.user.id, do, "Roll back"), ephemeral=True)

    @app_commands.command(name="history", description="Recent permission change batches")
    @require_level("mod")
    async def history(self, interaction: discord.Interaction):
        rows = await self.bot.db.list_batches(self.bot.guild.id, 15)
        lines = [f"#{r['id']} • {ts_fmt(r['ts'])} • {r['source']} • **{r['status']}** • {r['summary']} • by <@{r['actor_id']}>"
                 for r in rows] or ["No batches yet."]
        await send_pages(interaction, "Change history", lines)

    @app_commands.command(name="assign", description="Put a member into a Level (replaces other Level roles)")
    @app_commands.autocomplete(trust_level=_trust_level_ac)
    @require_level("admin")
    async def assign(self, interaction: discord.Interaction, member: discord.Member, trust_level: str):
        ids = self.bot.trust_level_role_ids()
        if trust_level not in ids:
            raise app_commands.CheckFailure(f"Level `{trust_level}` has no role on this server.")
        g = self.bot.guild
        target = g.get_role(ids[trust_level])
        if target >= g.me.top_role:
            raise app_commands.CheckFailure(f"@{target.name} is not below the bot's top role; move the bot role higher.")
        if interaction.user.id != g.owner_id and target >= interaction.user.top_role:
            raise app_commands.CheckFailure("You cannot assign a role equal to or above your own top role.")
        self.bot.guard("role_assign", interaction.user.id)
        remove = [r for r in member.roles if r.id in ids.values() and r.id != target.id]
        reason = bot_reason(interaction.user.display_name, interaction.user.id, f"trust_level assign {trust_level}")
        if remove:
            await member.remove_roles(*remove, reason=reason)
        await member.add_roles(target, reason=reason)
        await interaction.response.send_message(
            f"✅ {member.mention} is now in **{trust_level}** (@{target.name})" + (f"; removed {', '.join('@' + r.name for r in remove)}" if remove else ""),
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="reload", description="Reload config/server.yaml without restarting")
    @require_level("owner")
    async def reload(self, interaction: discord.Interaction):
        try:
            cfg = self.bot.reload_config()
        except Exception as e:  # noqa: BLE001
            raise app_commands.CheckFailure(f"Config invalid, kept the old one: {e}") from e
        await self.bot.db.add_event(type="config_reloaded", category="security", guild_id=interaction.guild_id,
                                    actor_id=interaction.user.id, actor_name=interaction.user.display_name,
                                    actor_confidence="confirmed", source="bot")
        await interaction.response.send_message(
            f"✅ Reloaded: {len(cfg.baseline.trust_levels)} trust levels, {len(cfg.baseline.channels)} channel rules, "
            f"auto-heal {'ON' if cfg.autoheal.enabled else 'off'}.", ephemeral=True)

    # ---------------------------------------------------------- auto-heal
    @tasks.loop(minutes=60)
    async def autoheal(self):
        cfg = self.bot.cfg.autoheal
        if not cfg.enabled or not self.bot.guild:
            return
        try:
            await self.autoheal_once()
        except Exception:  # noqa: BLE001
            log.exception("auto-heal failed")

    async def autoheal_once(self, now=None) -> str:
        """One auto-heal evaluation. Returns the decision (for logs/tests)."""
        from datetime import datetime, timedelta, timezone

        from ..autoheal_policy import decide
        from ..db import LogQuery, iso
        now = now or datetime.now(timezone.utc)
        cfg = self.bot.cfg.autoheal
        g = self.bot.guild
        if self.bot.safe_mode():
            return "safe_mode"
        plan = plan_repair(self.bot.model(), self.bot.cfg, self.bot.user.id, codes=AUTOHEAL_CODES)
        import time as _t
        accepted = set(await self.bot.db.kv_get("autoheal:accepted", []))
        snooze = await self.bot.db.kv_get("autoheal:snooze", {})
        plan.changes = [c for c in plan.changes if c.describe() not in accepted and snooze.get(c.describe(), 0) < _t.time()]
        dangerous = any(((c.after & ~c.before) if c.kind == "role_perms" else ((c.after or [0, 0])[0] & ~(c.before or [0, 0])[0]))
                        & F.value_of(F.DANGEROUS) for c in plan.changes)
        # owner changes to the EXACT object a repair would touch, made after the approved baseline, are intentional
        since = now - timedelta(hours=24)
        base_ts = await self.bot.db.kv_get("baseline:ts")
        if base_ts:
            since = max(since, datetime.fromisoformat(base_ts))
        recent = await self.bot.db.query_events(LogQuery(guild_id=g.id, category="structure", limit=500, since=iso(since)))
        owners = self.bot.bot_owner_ids() | {g.owner_id}

        def same_object(e, c) -> bool:
            if c.kind == "role_perms":
                return e["type"].startswith("role_") and c.target_id in (e["role_id"], e["target_id"])
            return e["type"].startswith("overwrite_") and e["channel_id"] == c.channel_id and c.target_id in (e["role_id"], e["target_id"])

        owner_touched = any(e["actor_id"] in owners and same_object(e, c) for e in recent for c in plan.changes)
        pending = await self.bot.db.kv_get("autoheal:pending")
        decision, new_pending = decide([c.describe() for c in plan.changes], pending, now, safe=plan.safe,
                                       dangerous=dangerous, n_changes=len(plan.changes), max_changes=cfg.max_changes,
                                       owner_touched=owner_touched, grace_s=cfg.interval_minutes * 60 * 0.66)
        await self.bot.db.kv_set("autoheal:pending", new_pending)
        guardian = self.bot.get_cog("Guardian")
        if decision == "apply":
            res = await apply_changes(self.bot, plan.changes, source="autoheal", actor=None,
                                      summary=f"auto-heal: {len(plan.changes)} change(s)")
            if guardian:
                await guardian.alert("WARNING" if res["status"] == "applied" else "CRITICAL",
                                     f"Auto-heal repaired drift ({len(plan.changes)} change(s)) — {res['status']}",
                                     "\n".join(c.describe() for c in plan.changes), key=f"autoheal:{res['batch_id']}",
                                     kind="Auto-heal", actor_name="the bot",
                                     status=f"verified {res['verified']}/{len(plan.changes)} against Discord",
                                     recommendation=f"Undo: /baseline rollback batch_id:{res['batch_id']}", post=True)
        elif decision == "skip_owner" and guardian:
            from .guardian import OwnerDecisionView
            await self.bot.db.kv_set("autoheal:decision", {"keys": [c.describe() for c in plan.changes],
                                                           "changes": [c.to_dict() for c in plan.changes]})
            await guardian.alert("WARNING", "You changed the approved baseline — auto-heal paused for these targets",
                                 "\n".join(c.describe() for c in plan.changes[:8]),
                                 key=f"autoheal-owner:{sorted(c.describe() for c in plan.changes)}", kind="Baseline decision",
                                 status="⏸️ auto-heal paused for these exact targets",
                                 recommendation="Accept keeps your change; Revert restores the approved baseline.",
                                 post=True, view=OwnerDecisionView())
        elif decision == "skip_unsafe" and guardian:
            why = ("the owner changed these objects recently — treated as intentional" if decision == "skip_owner"
                   else "the fix is not small/safe enough for automatic repair")
            await guardian.alert("WARNING", f"Drift from the approved baseline not auto-healed: {why}",
                                 "\n".join(c.describe() for c in plan.changes[:8]), key=f"autoheal-skip:{sorted(c.describe() for c in plan.changes)}",
                                 kind="Auto-heal", status="🔴 drift present",
                                 recommendation="If intentional, update config/server.yaml (the baseline); otherwise `/baseline repair`.")
        log.info("auto-heal decision: %s (%d change(s))", decision, len(plan.changes))
        return decision



def _result_embed(res: dict, title: str = "Repair") -> discord.Embed:
    ok = res["status"] == "applied"
    desc = [f"**Status:** {res['status']}  •  **Verified against Discord:** {res['verified']}",
            f"Batch #{res['batch_id']} • pre-change snapshot #{res['snapshot_id']}",
            f"Undo with `/baseline rollback batch_id:{res['batch_id']}`"]
    for m in res.get("mismatched", [])[:10]:
        desc.append(f"⚠️ {m['change']} — {m.get('error') or 'not reflected in Discord'}")
    return discord.Embed(title=f"{title} {'✅' if ok else '⚠️'}", description="\n".join(desc),
                         color=COLORS["OK"] if ok else COLORS["WARNING"])


async def setup(bot):
    await bot.add_cog(Baseline(bot))
