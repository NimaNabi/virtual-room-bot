"""Offline wiring checks: every module imports, every cog loads, command tree is sane."""
import importlib
from pathlib import Path

import pytest

from vrbot.bot import MODULES, ServerBot
from vrbot.config import Settings


@pytest.mark.parametrize("mod", MODULES)
def test_module_imports(mod):
    importlib.import_module(mod)


async def test_bot_loads_all_cogs_offline(tmp_path: Path):
    s = Settings(token=None, guild_id=None, owner_ids=[], data_dir=tmp_path, config_path=tmp_path / "server.yaml",
                 log_level="INFO", lavalink_uri=None, lavalink_password=None, ai_base_url=None, ai_api_key=None,
                 ai_model="default", ai_fallback_models=[], message_content_intent=False)
    bot = ServerBot(s)
    # setup_hook without connecting to Discord (tasks are started but never tick)
    try:
        await bot.setup_hook()
        failed = {k: v for k, v in bot.module_status.items() if v not in ("loaded", "ok")}
        assert not failed, failed
        names = {c.name for c in bot.tree.get_commands()}
        assert {"logs", "permissions", "server", "baseline", "backup", "mod", "music", "ai", "guardian", "stats", "voice", "help", "owner_room", "tier", "access", "guest", "tonight", "teams", "menu", "setup"} <= names
        from discord import AppCommandType
        slash = bot.tree.get_commands(type=AppCommandType.chat_input)
        users = bot.tree.get_commands(type=AppCommandType.user)
        assert len(slash) <= 30          # Discord allows 100 top-level; we keep a small, grouped surface
        assert len(users) <= 5           # Discord limit: 5 user context-menu commands
        assert all(len(getattr(c, "commands", [])) <= 25 for c in slash)  # Discord limit: 25 subcommands per group
    finally:  # never leave background loops running (a failing assert must not hang the suite)
        bot.heartbeat.cancel()
        await bot.db.close()


def test_discord_http_methods_used_for_verification_exist():
    # repair.py verifies changes by re-reading Discord over REST with these calls
    from discord.http import HTTPClient
    for name in ("get_roles", "get_all_guild_channels", "edit_channel_permissions", "delete_channel_permissions"):
        assert hasattr(HTTPClient, name), name


def test_intents_are_least_privilege(tmp_path):
    s = Settings(token=None, guild_id=None, owner_ids=[], data_dir=tmp_path, config_path=Path("x.yaml"),
                 log_level="INFO", lavalink_uri=None, lavalink_password=None, ai_base_url=None, ai_api_key=None,
                 ai_model="default", ai_fallback_models=[], message_content_intent=False)
    i = ServerBot(s).intents
    assert i.members and i.voice_states and i.moderation and i.guilds
    assert not i.message_content and not i.presences and not i.dm_messages
