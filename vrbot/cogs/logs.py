"""/logs — searchable server history."""
from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from ..bot import require_level
from ..db import LogQuery, iso, parse_duration
from ..events import CATEGORY
from ..render import event_line
from ..ui import send_pages

TYPE_CHOICES = sorted(set(CATEGORY) | {
    "channel_create", "channel_update", "channel_delete", "overwrite_create", "overwrite_update",
    "overwrite_delete", "role_create", "role_update", "role_delete", "guild_update", "bot_add"})
ACTION_GROUPS = {
    "ban": ["member_ban"], "unban": ["member_unban"], "kick": ["member_kick"],
    "timeout": ["timeout_add", "timeout_remove"], "leave": ["member_leave"], "join": ["member_join"],
    "roles": ["role_add", "role_remove"], "nick": ["nick_change"],
    "voice_mod": ["voice_disconnect", "voice_server_mute", "voice_server_unmute", "voice_server_deafen",
                  "voice_server_undeafen"],
    "channels": ["channel_create", "channel_update", "channel_delete"],
    "overwrites": ["overwrite_create", "overwrite_update", "overwrite_delete"],
    "role_changes": ["role_create", "role_update", "role_delete"],
    "messages": ["message_delete", "message_bulk_delete", "message_edit"],
}


def _since(text: str | None) -> str | None:
    if not text:
        return None
    return iso(datetime.now(timezone.utc) - parse_duration(text))


# hidden from members without Moderate Members in Discord's command picker; bot authz still enforces
@app_commands.default_permissions(moderate_members=True)
class Logs(commands.GroupCog, group_name="logs", group_description="Search the server history"):
    def __init__(self, bot):
        self.bot = bot
        super().__init__()

    async def _run(self, interaction: discord.Interaction, title: str, q: LogQuery):
        await interaction.response.defer(ephemeral=True, thinking=True)
        q.guild_id = self.bot.guild.id
        rows = await self.bot.db.query_events(q)
        if not self.bot.cfg.privacy.is_owner_like(interaction.user.id, self.bot.guild.owner_id):
            from ..perms.privacy import redact_for_staff, visible_channel_ids
            rows = redact_for_staff(rows, visible_channel_ids(self.bot.model(), interaction.user.id))
        lines = [event_line(r) for r in rows] or ["No matching events."]
        await send_pages(interaction, f"{title} — {len(rows)} event(s)", lines)

    @app_commands.command(name="recent", description="Most recent events")
    @app_commands.describe(limit="How many (default 25)", category="Filter by category")
    @app_commands.choices(category=[app_commands.Choice(name=c, value=c) for c in
                                    ("membership", "voice", "moderation", "structure", "message", "security", "bot")])
    @require_level("mod")
    async def recent(self, interaction: discord.Interaction, limit: app_commands.Range[int, 1, 100] = 25,
                     category: str | None = None):
        await self._run(interaction, "Recent events", LogQuery(limit=limit, category=category))

    @app_commands.command(name="user", description="Everything that happened to or was done by a member")
    @app_commands.describe(since="e.g. 24h, 7d, 4w")
    @require_level("mod")
    async def user(self, interaction: discord.Interaction, member: discord.User, since: str | None = None,
                   limit: app_commands.Range[int, 1, 200] = 40):
        await self._run(interaction, f"Events for {member.display_name}",
                        LogQuery(user_id=member.id, since=_since(since), limit=limit))

    @app_commands.command(name="moderator", description="Actions performed by a member (moderator view)")
    @require_level("mod")
    async def moderator(self, interaction: discord.Interaction, member: discord.User, since: str | None = "7d",
                        limit: app_commands.Range[int, 1, 200] = 50):
        await self._run(interaction, f"Actions by {member.display_name}",
                        LogQuery(actor_id=member.id, since=_since(since), limit=limit))

    @app_commands.command(name="action", description="Filter by kind of action")
    @app_commands.choices(action=[app_commands.Choice(name=k, value=k) for k in ACTION_GROUPS])
    @require_level("mod")
    async def action(self, interaction: discord.Interaction, action: str, since: str | None = "30d",
                     member: discord.User | None = None, limit: app_commands.Range[int, 1, 200] = 40):
        await self._run(interaction, f"{action} events", LogQuery(types=ACTION_GROUPS[action], since=_since(since),
                                                                  target_id=member.id if member else None, limit=limit))

    @app_commands.command(name="voice", description="Voice activity for a member or channel")
    @require_level("mod")
    async def voice(self, interaction: discord.Interaction, member: discord.User | None = None,
                    channel: discord.VoiceChannel | None = None, since: str | None = "7d",
                    limit: app_commands.Range[int, 1, 200] = 50):
        await self._run(interaction, "Voice activity", LogQuery(category="voice", target_id=member.id if member else None,
                                                                channel_id=channel.id if channel else None,
                                                                since=_since(since), limit=limit))

    @app_commands.command(name="since", description="Everything within a time window (e.g. 24h)")
    @require_level("mod")
    async def since(self, interaction: discord.Interaction, window: str,
                    type: str | None = None, limit: app_commands.Range[int, 1, 200] = 60):  # noqa: A002
        await self._run(interaction, f"Events in the last {window}",
                        LogQuery(since=_since(window), types=[type] if type else [], limit=limit))

    @since.autocomplete("type")
    async def _type_ac(self, interaction, current: str):
        return [app_commands.Choice(name=t, value=t) for t in TYPE_CHOICES if current.lower() in t][:25]

    @app_commands.command(name="channel", description="What happened to/in a channel")
    @require_level("mod")
    async def channel(self, interaction: discord.Interaction, channel: discord.abc.GuildChannel,
                      since: str | None = "30d", limit: app_commands.Range[int, 1, 200] = 40):
        await self._run(interaction, f"Events for #{channel.name}",
                        LogQuery(channel_id=channel.id, since=_since(since), limit=limit))

    @app_commands.command(name="search", description="Free-text search (names, reasons, roles, channels)")
    @require_level("mod")
    async def search(self, interaction: discord.Interaction, text: str, since: str | None = "90d",
                     limit: app_commands.Range[int, 1, 200] = 40):
        await self._run(interaction, f"Search: {text}", LogQuery(text=text, since=_since(since), limit=limit))

    @app_commands.command(name="export", description="Download events as CSV or JSON")
    @app_commands.choices(fmt=[app_commands.Choice(name="csv", value="csv"), app_commands.Choice(name="json", value="json")])
    @require_level("admin")
    async def export(self, interaction: discord.Interaction, since: str = "30d", fmt: str = "csv",
                     member: discord.User | None = None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        rows = await self.bot.db.query_events(LogQuery(guild_id=self.bot.guild.id, since=_since(since),
                                                       user_id=member.id if member else None, limit=5000))
        if fmt == "json":
            data = json.dumps(rows, indent=1, default=str).encode()
        else:
            buf = io.StringIO()
            if rows:
                w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
                w.writeheader()
                w.writerows(rows)
            data = buf.getvalue().encode()
        f = discord.File(io.BytesIO(data), filename=f"vrbot-events-{since}.{fmt}")
        await interaction.followup.send(f"{len(rows)} event(s).", file=f, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Logs(bot))
