"""/help — commands the current user can actually use, grouped and explained."""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from ..authz import Level
from ..ui import COLORS

M, T, MOD, A, O = Level.MEMBER, Level.TRUSTED, Level.MOD, Level.ADMIN, Level.OWNER

# (group, emoji, blurb, {subcommand: minimum level}); unlisted subcommands default to the group's first level
CATALOG = [
    ("permissions", "🩺", "Why can/can't someone see or do something", {"why": M, "privacy": O, "*": MOD}),
    ("music", "🎶", "Play music in your voice channel", {"*": M}),
    ("voice", "🔊", "Your temporary room & moving people", {"setup": O, "disable": O, "move": T, "*": M}),
    ("owner_room", "🔒", "Owner-only private voice", {"*": O}),
    ("baseline", "⭕", "Access groups & baseline (owner)", {"*": O}),
    ("ai", "🤖", "Ask about the server in plain language", {"status": M, "*": MOD}),
    ("server", "🏠", "Status and full health check", {"*": MOD}),
    ("guardian", "🛡️", "Watchdog alerts and Safe Mode", {"status": MOD, "alerts": MOD, "*": O}),
    ("logs", "📜", "Searchable server history", {"export": A, "*": MOD}),
    ("stats", "📊", "History in numbers", {"*": MOD}),
    ("mod", "🔨", "Moderation", {"*": MOD}),
    ("backup", "💾", "Server structure snapshots", {"create": A, "restore": O, "*": MOD}),
]


def allowed(level: Level, rules: dict, sub: str) -> bool:
    return level >= rules.get(sub, rules.get("*", MOD))


class Help(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        from ..guide import GuideView
        self.bot.add_view(GuideView())

    @app_commands.command(name="help", description="Open the menu — everything works with taps")
    async def help(self, interaction: discord.Interaction):
        await self.bot.get_cog("ControlCenter").open(interaction, "home")


EXTRA = [("tier", "🧭", "Trust levels (private)", {"*": O}), ("access", "⏱️", "Temporary mod/admin", {"*": O}),
         ("guest", "🎟️", "Guest invites", {"*": O})]


async def setup(bot):
    await bot.add_cog(Help(bot))
