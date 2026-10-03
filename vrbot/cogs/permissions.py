"""/permissions — the Permission Doctor."""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from ..authz import Level
from ..bot import perm_choices, require_level
from ..perms import flags as F
from ..perms.audit import SEV_ORDER, audit, expectations_for, plan_repair, synthetic
from ..perms.engine import compute, explain
from ..perms.snapshot import member_from_discord
from ..ui import COLORS, SEV_ICON, send_pages


def render_findings(findings, limit_info: int = 15) -> list[str]:
    lines: list[str] = []
    infos = 0
    for sev in ("CRITICAL", "WARNING", "INFO"):
        group = [f for f in findings if f.severity == sev]
        if not group:
            continue
        lines.append(f"**{SEV_ICON[sev]} {sev} ({len(group)})**")
        for f in group:
            if sev == "INFO":
                infos += 1
                if infos > limit_info:
                    continue
            fix = " 🔧" if f.fixable else ""
            lines.append(f"• {f.title}{fix}" + (f"\n  ↳ {f.detail}" if f.detail else ""))
        if sev == "INFO" and infos > limit_info:
            lines.append(f"…and {infos - limit_info} more INFO item(s).")
        lines.append("")
    if not findings:
        lines.append("✅ No problems found.")
    else:
        lines.append("🔧 = can be repaired automatically with `/baseline repair` (dry-run first).")
    return lines


class Permissions(commands.GroupCog, group_name="permissions", group_description="Permission Doctor"):
    def __init__(self, bot):
        self.bot = bot
        super().__init__()

    @app_commands.command(name="why", description="Explain exactly why someone can/can't do something in a channel")
    @app_commands.describe(member="Who (default: you)", channel="Where", permission="Specific permission (optional)")
    async def why(self, interaction: discord.Interaction, channel: discord.abc.GuildChannel,
                  member: discord.Member | None = None, permission: str | None = None):
        member = member or interaction.user
        if member.id != interaction.user.id and self.bot.level_of(interaction.user) < Level.MOD:
            raise app_commands.CheckFailure("You can diagnose yourself; diagnosing others needs Moderator access.")
        g = self.bot.model()
        m = g.members.get(member.id) or member_from_discord(member)
        ch = g.channels[channel.id]
        owner_view = self.bot.cfg.privacy.is_owner_like(interaction.user.id, g.owner_id)
        if not owner_view and self.bot.level_of(interaction.user) < Level.MOD:
            from ..perms.engine import compute as _c
            from ..perms import flags as _F
            if not _c(g, m, ch).value & _F.FLAGS["view_channel"]:
                await interaction.response.send_message(
                    "🔒 You don't have access to that area. Access to private areas is assigned individually by the "
                    "server owner.", ephemeral=True)
                return
        try:
            ex = explain(g, m, ch, [permission] if permission else None)
        except ValueError as e:
            raise app_commands.CheckFailure(str(e)) from e
        ok = all(t.allowed for t in ex.traces)
        await send_pages(interaction, "Bot permission diagnosis", ex.render(g).split("\n"),
                         color=COLORS["OK"] if ok else COLORS["WARNING"])

    @why.autocomplete("permission")
    async def _perm_ac(self, interaction, current: str):
        return perm_choices(current)

    @app_commands.command(name="user", description="Where a member can and cannot go, and their powerful permissions")
    @require_level("mod")
    async def user(self, interaction: discord.Interaction, member: discord.Member):
        g = self.bot.model()
        m = g.members[member.id]
        base = compute(g, m)
        lines = [f"**Roles:** {', '.join('@' + g.roles[r].name for r in sorted(m.role_ids, key=lambda r: -g.roles[r].position)) or 'none'}"]
        if base.owner:
            lines.append("👑 Server owner — has everything.")
        elif base.admin:
            lines.append("⚠️ **Administrator** — bypasses every channel overwrite.")
        danger = [n for n in F.names_of(base.value) if n in F.DANGEROUS]
        if danger and not base.admin:
            lines.append(f"**Powerful permissions:** {', '.join(F.pretty(n) for n in danger)}")
        if m.timed_out:
            lines.append("⏳ Currently timed out.")
        lines.append("")
        can, cannot = [], []
        for ch in sorted(g.channels.values(), key=lambda c: (c.parent_id or 0, c.position)):
            if ch.type == "category":
                continue
            v = compute(g, m, ch).value
            if not v & F.FLAGS["view_channel"]:
                cannot.append(f"#{ch.name}")
                continue
            if ch.is_voice:
                flags = ("🔊" if v & F.FLAGS["connect"] else "🚫connect") + ("🎙️" if v & F.FLAGS["speak"] else " 🔇no-speak")
            else:
                flags = "✍️" if v & F.FLAGS["send_messages"] else "👀 read-only"
            can.append(f"#{ch.name} {flags}")
        lines.append(f"**Can see ({len(can)}):** " + ", ".join(can))
        lines.append("")
        lines.append(f"**Cannot see ({len(cannot)}):** " + (", ".join(cannot) or "—"))
        lines.append("\nUse `/permissions why` for the exact reason on a channel.")
        await send_pages(interaction, f"Access for {member.display_name}", lines)

    @app_commands.command(name="role", description="What a role grants and where it has overwrites")
    @require_level("mod")
    async def role(self, interaction: discord.Interaction, role: discord.Role):
        g = self.bot.model()
        r = g.roles[role.id]
        names = F.names_of(r.permissions)
        danger = [n for n in names if n in F.DANGEROUS]
        lines = [f"**Position:** {r.position}  •  **Members:** {r.member_count}",
                 f"**Server-level permissions ({len(names)}):** {', '.join(F.pretty(n) for n in names) or 'none'}"]
        if danger:
            lines.append(f"⚠️ **Powerful:** {', '.join(F.pretty(n) for n in danger)}")
        lines.append("\n**Channel overwrites:**")
        n = 0
        for ch in sorted(g.channels.values(), key=lambda c: c.position):
            ow = ch.overwrites.get(r.id)
            if ow:
                n += 1
                s = []
                if ow.allow:
                    s.append("✅ " + ", ".join(F.pretty(x) for x in F.names_of(ow.allow)))
                if ow.deny:
                    s.append("❌ " + ", ".join(F.pretty(x) for x in F.names_of(ow.deny)))
                lines.append(f"• {ch.mention}: {' | '.join(s)}")
        if not n:
            lines.append("none")
        sm = synthetic(r.id if r.id != g.id else None)
        visible = [c.name for c in g.channels.values() if c.type != "category" and compute(g, sm, c).value & F.FLAGS["view_channel"]]
        lines.append(f"\n**A member with only this role (+@everyone) sees {len(visible)} channel(s):** {', '.join('#' + v for v in visible)}")
        await send_pages(interaction, f"Role @{role.name}", lines)

    @app_commands.command(name="channel", description="Overwrites on a channel and who can use it")
    @require_level("mod")
    async def channel(self, interaction: discord.Interaction, channel: discord.abc.GuildChannel):
        g = self.bot.model()
        ch = g.channels[channel.id]
        lines = []
        if ch.parent_id:
            synced = g.is_synced(ch)
            lines.append(f"**Category:** {g.channels[ch.parent_id].name} — {'synced ✅' if synced else 'NOT synced ⚠️'}")
        lines.append("**Overwrites:**")
        for tid, ow in ch.overwrites.items():
            who = "@everyone" if tid == g.id else (("@" + g.roles[tid].name) if tid in g.roles else
                                                   (g.members[tid].name + " (member)" if tid in g.members else f"<deleted {tid}>"))
            s = []
            if ow.allow:
                s.append("✅ " + ", ".join(F.pretty(x) for x in F.names_of(ow.allow)))
            if ow.deny:
                s.append("❌ " + ", ".join(F.pretty(x) for x in F.names_of(ow.deny)))
            lines.append(f"• {who}: {' | '.join(s) or '(neutral)'}")
        if not ch.overwrites:
            lines.append("none")
        lines.append("\n**Trust levels (synthetic member with only that role):**")
        trust_levels = self.bot.trust_level_role_ids()
        exp = expectations_for(g, self.bot.cfg.baseline, self.bot.cfg.privacy).get(ch.id, {})
        for key, rid in list(trust_levels.items()) + [("default", None)]:
            v = compute(g, synthetic(rid), ch).value
            keys = ["view_channel", "connect", "speak"] if ch.is_voice else ["view_channel", "send_messages", "read_message_history"]
            got = " ".join(("✅" if v & F.FLAGS[p] else "❌") + F.pretty(p) for p in keys)
            e = exp.get(key)
            mism = ""
            if e:
                bad = [p for p, want in e.items() if bool(v & F.FLAGS[p]) != want]
                mism = " ⚠️ differs from baseline: " + ", ".join(F.pretty(p) for p in bad) if bad else " ✔️ matches baseline"
            lines.append(f"• **{key}**: {got}{mism}")
        await send_pages(interaction, f"{ch.mention} permissions", lines)

    @app_commands.command(name="audit", description="Full permission audit (all severities)")
    @require_level("mod")
    async def audit_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        findings = audit(self.bot.model(), self.bot.cfg, self.bot.user.id, list(self.bot.bot_owner_ids()))
        worst = min((SEV_ORDER[f.severity] for f in findings), default=3)
        color = [COLORS["CRITICAL"], COLORS["WARNING"], COLORS["INFO"], COLORS["OK"]][worst]
        await send_pages(interaction, f"Permission audit — {len(findings)} finding(s)", render_findings(findings, 40), color=color)

    @app_commands.command(name="problems", description="Only CRITICAL and WARNING permission problems")
    @require_level("mod")
    async def problems(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        findings = [f for f in audit(self.bot.model(), self.bot.cfg, self.bot.user.id, list(self.bot.bot_owner_ids()))
                    if f.severity != "INFO"]
        await send_pages(interaction, f"Permission problems — {len(findings)}", render_findings(findings),
                         color=COLORS["CRITICAL"] if findings else COLORS["OK"])

    @app_commands.command(name="diff", description="Dry-run: what repairing drift from the Level baseline would change")
    @require_level("mod")
    async def diff(self, interaction: discord.Interaction, trust_level: str | None = None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        plan = plan_repair(self.bot.model(), self.bot.cfg, self.bot.user.id, scope=trust_level)
        await send_pages(interaction, "Baseline diff (dry run — nothing changed)", render_plan(plan))

    @diff.autocomplete("trust_level")
    async def _trust_level_ac(self, interaction, current: str):
        keys = list(self.bot.cfg.baseline.trust_levels) + ["default"]
        return [app_commands.Choice(name=k, value=k) for k in keys if current.lower() in k]

    @app_commands.command(name="privacy", description="Privacy Doctor: prove no floor is visible to a lower trust tier")
    @require_level("owner")
    async def privacy(self, interaction: discord.Interaction):
        from ..perms.privacy import privacy_audit, visibility_matrix
        await interaction.response.defer(ephemeral=True, thinking=True)
        g = self.bot.model()
        p = self.bot.cfg.privacy
        if not p.tiers:
            raise app_commands.CheckFailure("No privacy tiers configured.")
        passes = {tuple(x) for x in g.settings.get("temporary_passes", [])}
        leaks = privacy_audit(g, self.bot.cfg, self.bot.user.id, passes)
        tiers = sorted(p.tiers.items(), key=lambda kv: kv[1].rank)
        lines = ["**Floors** (most private first): " + " → ".join(f"{t.label or k}" for k, t in tiers), ""]
        for k, t in tiers:
            unauthorized = [x for x in leaks if x.code in ("privacy_leak", "privacy_tier_leak")
                            and f"({k})" in x.title]
            lines.append(f"{'✅' if not unauthorized else '🔴'} **{t.label or k}** — visible to unauthorized: {len(unauthorized)}")
        lines.append("")
        mat = visibility_matrix(g, self.bot.cfg)
        order = [k for k, _ in tiers]
        lines.append("```\n" + "viewer".ljust(10) + " ".join(k[:8].ljust(8) for k in order))
        for viewer, row in mat.items():
            lines.append(viewer[:9].ljust(10) + " ".join(("✅" if row.get(a) else "—").ljust(8) for a in order))
        lines.append("```")
        bad = [x for x in leaks if x.severity != "INFO"]
        lines.append(f"**Findings:** {len(bad)} (+{len(leaks) - len(bad)} info)")
        lines += [f"{'🔴' if x.severity == 'CRITICAL' else '🟡'} {x.title}\n  ↳ {x.detail}" for x in bad[:20]]
        if not bad:
            lines.append("✅ No privacy leaks: every private floor is invisible to lower tiers (View Channel denied, so "
                         "voice presence and voice-chat text are hidden too).")
        await send_pages(interaction, "🔒 Privacy Doctor", lines,
                         color=COLORS["CRITICAL"] if any(x.severity == "CRITICAL" for x in bad) else COLORS["OK"])

    @app_commands.command(name="matrix", description="Grid of channels × trust levels (view/send/connect)")
    @require_level("mod")
    async def matrix(self, interaction: discord.Interaction):
        g = self.bot.model()
        trust_levels = list(self.bot.trust_level_role_ids().items()) + [("default", None)]
        header = "channel".ljust(22) + " ".join(k[:8].ljust(8) for k, _ in trust_levels)
        rows = [header, "-" * len(header)]
        for ch in sorted(g.channels.values(), key=lambda c: ((g.channels[c.parent_id].position if c.parent_id in g.channels else c.position), c.parent_id or 0, c.position)):
            if ch.type == "category":
                rows.append(f"[{ch.name[:20]}]")
                continue
            cells = []
            for _, rid in trust_levels:
                v = compute(g, synthetic(rid), ch).value
                if not v & F.FLAGS["view_channel"]:
                    cells.append("-".ljust(8))
                elif ch.is_voice:
                    cells.append(("C" if v & F.FLAGS["connect"] else "c") + ("S" if v & F.FLAGS["speak"] else "s") + "      ")
                else:
                    cells.append(("W" if v & F.FLAGS["send_messages"] else "R").ljust(8))
            rows.append(("#" + ch.name)[:21].ljust(22) + " ".join(cells))
        legend = "W=read+write R=read-only -=hidden | voice: C/c=connect yes/no, S/s=speak yes/no"
        text = "\n".join(rows)
        chunks = [text[i:i + 3800] for i in range(0, len(text), 3800)]
        await send_pages(interaction, "Permission matrix", [f"```\n{c}\n```" for c in chunks] + [legend])


def render_plan(plan) -> list[str]:
    lines = []
    if not plan.changes:
        lines.append("✅ Nothing to change: the server already matches the baseline for this scope.")
    else:
        lines.append(f"**Proposed changes ({len(plan.changes)}):**")
        lines += [f"• {c.describe()}" for c in plan.changes]
        lines.append("")
        lines.append(f"**Resolves ({len(plan.resolved)}):** " + "; ".join(f.title for f in plan.resolved[:12]))
    if plan.introduced:
        lines.append(f"\n⚠️ **Would introduce {len(plan.introduced)} new problem(s):**")
        lines += [f"• {f.title}" for f in plan.introduced[:10]]
    if plan.blocked:
        lines.append("\n⛔ **The bot cannot apply this yet:**")
        lines += [f"• {b}" for b in plan.blocked]
    unfixable = [f for f in plan.remaining if not f.fixable and f.severity != "INFO"]
    if unfixable:
        lines.append(f"\nℹ️ {len(unfixable)} problem(s) need a human (not auto-repairable), e.g. role ordering.")
    return lines


async def setup(bot):
    await bot.add_cog(Permissions(bot))
