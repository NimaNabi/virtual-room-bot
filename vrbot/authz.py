"""Who may do what. Pure functions so they are unit-testable."""
from __future__ import annotations

from enum import IntEnum


class Level(IntEnum):
    MEMBER = 0
    TRUSTED = 1   # Level 1
    MOD = 2
    ADMIN = 3
    OWNER = 4

    @classmethod
    def parse(cls, s: str) -> "Level":
        return {"member": cls.MEMBER, "trusted": cls.TRUSTED, "mod": cls.MOD, "moderator": cls.MOD,
                "admin": cls.ADMIN, "owner": cls.OWNER}[s.lower()]


def level_for(*, user_id: int, role_ids: set[int], guild_owner_id: int, owner_ids: set[int],
              admin_role_ids: set[int], mod_role_ids: set[int], trusted_role_ids: set[int],
              has_administrator: bool, has_mod_perms: bool) -> Level:
    if user_id == guild_owner_id or user_id in owner_ids:
        return Level.OWNER
    if has_administrator or role_ids & admin_role_ids:
        return Level.ADMIN
    if role_ids & mod_role_ids or has_mod_perms:
        return Level.MOD
    if role_ids & trusted_role_ids:
        return Level.TRUSTED
    return Level.MEMBER


def moderation_block(*, actor_id: int, actor_top: int, actor_level: Level, target_id: int, target_top: int,
                     target_level: Level, bot_top: int, guild_owner_id: int, bot_id: int) -> str | None:
    """Return a human reason why actor may NOT act on target, or None if allowed."""
    if target_id == actor_id:
        return "You cannot moderate yourself."
    if target_id == bot_id:
        return "The bot will not moderate itself."
    if target_id == guild_owner_id:
        return "The server owner cannot be moderated."
    if target_level >= Level.OWNER:
        return "That user is a configured bot owner."
    if bot_top <= target_top:
        return "Their highest role is equal to or above the bot's highest role (Discord hierarchy). Move the bot role higher."
    if actor_id != guild_owner_id and actor_level < Level.OWNER and actor_top <= target_top:
        return "Their highest role is equal to or above yours."
    return None
