"""Discord permission bit definitions.

Source of truth: https://docs.discord.com/developers/topics/permissions
Kept independent from discord.py so the permission engine is pure and testable.
"""
from __future__ import annotations

FLAGS: dict[str, int] = {
    "create_instant_invite": 1 << 0,
    "kick_members": 1 << 1,
    "ban_members": 1 << 2,
    "administrator": 1 << 3,
    "manage_channels": 1 << 4,
    "manage_guild": 1 << 5,
    "add_reactions": 1 << 6,
    "view_audit_log": 1 << 7,
    "priority_speaker": 1 << 8,
    "stream": 1 << 9,
    "view_channel": 1 << 10,
    "send_messages": 1 << 11,
    "send_tts_messages": 1 << 12,
    "manage_messages": 1 << 13,
    "embed_links": 1 << 14,
    "attach_files": 1 << 15,
    "read_message_history": 1 << 16,
    "mention_everyone": 1 << 17,
    "use_external_emojis": 1 << 18,
    "view_guild_insights": 1 << 19,
    "connect": 1 << 20,
    "speak": 1 << 21,
    "mute_members": 1 << 22,
    "deafen_members": 1 << 23,
    "move_members": 1 << 24,
    "use_vad": 1 << 25,
    "change_nickname": 1 << 26,
    "manage_nicknames": 1 << 27,
    "manage_roles": 1 << 28,
    "manage_webhooks": 1 << 29,
    "manage_guild_expressions": 1 << 30,
    "use_application_commands": 1 << 31,
    "request_to_speak": 1 << 32,
    "manage_events": 1 << 33,
    "manage_threads": 1 << 34,
    "create_public_threads": 1 << 35,
    "create_private_threads": 1 << 36,
    "use_external_stickers": 1 << 37,
    "send_messages_in_threads": 1 << 38,
    "use_embedded_activities": 1 << 39,
    "moderate_members": 1 << 40,
    "view_creator_monetization_analytics": 1 << 41,
    "use_soundboard": 1 << 42,
    "create_guild_expressions": 1 << 43,
    "create_events": 1 << 44,
    "use_external_sounds": 1 << 45,
    "send_voice_messages": 1 << 46,
    "set_voice_channel_status": 1 << 48,
    "send_polls": 1 << 49,
    "use_external_apps": 1 << 50,
    "pin_messages": 1 << 51,
    "bypass_slowmode": 1 << 52,
}

ALL = 0
for _v in FLAGS.values():
    ALL |= _v
NONE = 0

# Friendly aliases accepted in config and commands.
ALIASES: dict[str, str] = {
    "view": "view_channel",
    "read": "view_channel",
    "read_messages": "view_channel",
    "send": "send_messages",
    "history": "read_message_history",
    "embed": "embed_links",
    "attach": "attach_files",
    "react": "add_reactions",
    "timeout": "moderate_members",
    "kick": "kick_members",
    "ban": "ban_members",
    "admin": "administrator",
    "manage_server": "manage_guild",
    "manage_emojis": "manage_guild_expressions",
    "vad": "use_vad",
    "video": "stream",
    "threads": "send_messages_in_threads",
    "slash": "use_application_commands",
}

# Permissions that only make sense at guild level (never meaningful in overwrites).
GUILD_ONLY = {
    "administrator", "kick_members", "ban_members", "manage_guild", "view_audit_log",
    "view_guild_insights", "change_nickname", "manage_nicknames", "moderate_members",
    "view_creator_monetization_analytics", "manage_guild_expressions", "create_guild_expressions",
}

# Permissions that give real control over the server or its members.
DANGEROUS = {
    "administrator", "manage_guild", "manage_roles", "manage_channels", "ban_members",
    "kick_members", "moderate_members", "manage_webhooks", "mention_everyone",
    "manage_messages", "manage_nicknames", "manage_guild_expressions", "manage_events",
    "manage_threads", "view_audit_log", "move_members", "mute_members", "deafen_members",
}
# Subset where a grant to the wrong role is always critical.
CRITICAL = {"administrator", "manage_guild", "manage_roles", "ban_members", "kick_members", "manage_webhooks"}

TEXT_SEND_DEPENDENT = {"mention_everyone", "send_tts_messages", "attach_files", "embed_links"}
VOICE_CONNECT_DEPENDENT = {
    "speak", "stream", "mute_members", "deafen_members", "move_members", "use_vad",
    "priority_speaker", "manage_channels", "use_soundboard", "use_external_sounds",
}
TIMEOUT_ALLOWED = {"view_channel", "read_message_history"}


def normalize(name: str) -> str:
    n = name.strip().lower().replace(" ", "_").replace("-", "_")
    n = ALIASES.get(n, n)
    if n not in FLAGS:
        raise ValueError(f"Unknown permission: {name!r}")
    return n


def value_of(names) -> int:
    v = 0
    for n in names:
        v |= FLAGS[normalize(n)]
    return v


def names_of(value: int) -> list[str]:
    return [n for n, b in FLAGS.items() if value & b]


def has(value: int, name: str) -> bool:
    return bool(value & FLAGS[normalize(name)])


def pretty(name: str) -> str:
    return name.replace("_", " ").title().replace("Vad", "Voice Activity")


# Common bundles for concise channel expectations in config.
LEVELS: dict[str, dict[str, bool]] = {
    "none": {"view_channel": False},
    "read": {"view_channel": True, "read_message_history": True, "send_messages": False},
    "archive": {"view_channel": True, "read_message_history": True, "send_messages": False, "connect": False, "speak": False},
    "write": {"view_channel": True, "read_message_history": True, "send_messages": True},
    "voice": {"view_channel": True, "connect": True, "speak": True},
    "listen": {"view_channel": True, "connect": True, "speak": False},
    "visible": {"view_channel": True},
}
