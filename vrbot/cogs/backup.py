"""/backup — server structure snapshots, diff and permission-level restore."""
from __future__ import annotations

import io
import json

import discord
from discord import app_commands
from discord.ext import commands

from ..authz import Level
from ..bot import require_level
from ..perms.model import Guild
from ..perms.snapshot import restore_changes, structural_diff
from ..repair import apply_changes
from ..ui import COLORS, ConfirmView, send_pages, ts_fmt


# hidden from members without Moderate Members in Discord's command picker; bot authz still enforces
@app_commands.default_permissions(moderate_members=True)
class Backup(commands.GroupCog, group_name="backup", group_description="Server structure snapshots"):
    def __init__(self, bot):
        self.bot = bot
        super().__init__()

    async def _load(self, snap_id: int) -> tuple[dict, Guild]:
        s = await self.bot.db.get_snapshot(snap_id)
        if not s or s["guild_id"] != self.bot.guild.id:
            raise app_commands.CheckFailure(f"Snapshot #{snap_id} not found.")
        return s, Guild.from_dict(s["data"])

    @app_commands.command(name="create", description="Snapshot roles, permissions, channels and overwrites now")
    @require_level("admin")
    async def create(self, interaction: discord.Interaction, label: str | None = None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        sid = await self.bot.take_snapshot("manual", label=label, created_by=interaction.user.id)
        s = await self.bot.db.get_snapshot(sid)
        path = self.bot.settings.data_dir / "backups"
        path.mkdir(parents=True, exist_ok=True)
        (path / f"snapshot-{sid}.json").write_text(json.dumps(s["data"], indent=1))
        await self.bot.db.add_event(type="backup_created", category="bot", guild_id=self.bot.guild.id,
                                    actor_id=interaction.user.id, actor_name=interaction.user.display_name,
                                    actor_confidence="confirmed", reason=label, details={"snapshot": sid}, source="bot")
        await interaction.followup.send(f"✅ Snapshot **#{sid}** saved ({s['summary']}). Also written to `data/backups/snapshot-{sid}.json`.",
                                        ephemeral=True)

    @app_commands.command(name="list", description="List snapshots")
    @require_level("mod")
    async def list_(self, interaction: discord.Interaction):
        rows = await self.bot.db.list_snapshots(self.bot.guild.id, 25)
        lines = [f"#{r['id']} • {ts_fmt(r['ts'])} • {r['kind']} • {r['label'] or ''} • {r['summary']}" for r in rows] \
            or ["No snapshots yet. `/backup create`"]
        await send_pages(interaction, "Snapshots", lines)

    @app_commands.command(name="inspect", description="Show what a snapshot contains (or download it)")
    @require_level("mod")
    async def inspect(self, interaction: discord.Interaction, snapshot_id: int, download: bool = False):
        s, g = await self._load(snapshot_id)
        roles = sorted(g.roles.values(), key=lambda r: -r.position)
        lines = [f"**Taken:** {ts_fmt(s['ts'])} ({s['kind']}) {s['label'] or ''}",
                 f"**Roles ({len(roles)}):** " + ", ".join("@" + r.name for r in roles),
                 f"**Channels ({len(g.channels)}):** " + ", ".join(c.mention for c in sorted(g.channels.values(), key=lambda c: c.position)),
                 f"**Overwrites:** {sum(len(c.overwrites) for c in g.channels.values())}",
                 f"**Settings:** {', '.join(f'{k}={v}' for k, v in g.settings.items())}"]
        f = discord.File(io.BytesIO(json.dumps(s["data"], indent=1).encode()), f"snapshot-{snapshot_id}.json") if download else None
        await send_pages(interaction, f"Snapshot #{snapshot_id}", lines, file=f)

    @app_commands.command(name="diff", description="What changed between a snapshot and now (or another snapshot)")
    @require_level("mod")
    async def diff(self, interaction: discord.Interaction, snapshot_id: int, other_snapshot_id: int | None = None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        _, old = await self._load(snapshot_id)
        if other_snapshot_id:
            _, new = await self._load(other_snapshot_id)
            label = f"#{other_snapshot_id}"
        else:
            new, label = self.bot.model(), "now"
        lines = structural_diff(old, new) or ["No structural differences."]
        await send_pages(interaction, f"Diff #{snapshot_id} → {label}", lines)

    @app_commands.command(name="restore", description="Restore role permissions + channel overwrites from a snapshot (dry run first)")
    async def restore(self, interaction: discord.Interaction, snapshot_id: int):
        if self.bot.level_of(interaction.user) < Level.OWNER:
            raise app_commands.CheckFailure("Restore is owner-only.")
        await interaction.response.defer(ephemeral=True, thinking=True)
        s, backup = await self._load(snapshot_id)
        changes, notes = restore_changes(self.bot.model(), backup)
        lines = [f"**Restore from #{snapshot_id} ({ts_fmt(s['ts'])}) — dry run.**",
                 "Scope: role permissions and channel permission overwrites of objects that still exist.", ""]
        if changes:
            lines += [f"• {c.describe()}" for c in changes]
        else:
            lines.append("✅ Permissions already match the snapshot.")
        if notes:
            lines += ["", "**Not restorable automatically:**"] + [f"• {n}" for n in notes[:20]]
        if not changes:
            await send_pages(interaction, "Restore", lines, color=COLORS["OK"])
            return

        async def do(i: discord.Interaction):
            res = await apply_changes(self.bot, changes, source="restore", actor=i.user,
                                      summary=f"restore from snapshot #{snapshot_id}: {len(changes)} change(s)")
            from .trust_level import _result_embed
            await i.followup.send(embed=_result_embed(res, "Restore"), ephemeral=True)

        await send_pages(interaction, "Restore — dry run", lines, color=COLORS["WARNING"])
        await interaction.followup.send(f"Apply {len(changes)} change(s)?",
                                        view=ConfirmView(interaction.user.id, do, f"Restore {len(changes)} change(s)"),
                                        ephemeral=True)


async def setup(bot):
    await bot.add_cog(Backup(bot))
