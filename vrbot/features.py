"""One registry of the bot's feature modules.

Each entry says what the module is, who may use it, whether it is on for THIS server, and how healthy it is. Help,
the owner's System screen and diagnostics are all generated from this list, so there is one source of truth instead
of several hand-written feature lists.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Feature:
    key: str
    emoji: str
    name: str
    help: str                                   # one line: how a member uses it (tap path)
    cog: str | None                             # cog that implements it (None = core)
    audience: str = "member"                    # member | owner
    enabled: Callable[[object], bool] = lambda bot: True
    setup: str = ""                             # what the owner must do to turn it on


def _cfg(bot, path: str, default=True):
    obj = bot.cfg
    for part in path.split("."):
        obj = getattr(obj, part, None)
        if obj is None:
            return default
    return bool(obj)


FEATURES: list[Feature] = [
    Feature("music", "🎵", "Music & radio", "Join a voice channel → Music → Quick Play, Search or Radio.", "Music",
            enabled=lambda b: _cfg(b, "music.enabled") and bool(b.settings.lavalink_uri),
            setup="Music needs the local Lavalink service (COMPOSE_PROFILES=music) and music.enabled."),
    Feature("rooms", "🔊", "Your own voice room", "Join ➕ Create Room, or My Room → Create. Lock, hide, rename, invite.",
            "VoiceRooms", enabled=lambda b: _cfg(b, "voice_rooms.enabled"), setup="Turn on Temporary rooms in /setup."),
    Feature("guests", "🎟️", "Bring a friend", "Friends → Bring someone in (one-time invite) or give access.", "Guests"),
    Feature("social", "🎮", "Tonight & teams", "Social → Tonight's plan, or split the voice channel into teams.", "Fun"),
    Feature("ai", "🤖", "AI assistant", "Ask questions about the server with /ask (moderators and up).", "AI",
            audience="owner", enabled=lambda b: _cfg(b, "ai.enabled", False) and bool(b.settings.ai_base_url),
            setup="Optional: set AI_ENABLED, AI_BASE_URL, AI_API_KEY and AI_MODEL in .env."),
    Feature("members", "🧭", "Trust levels & temporary access", "Owner → pick a member → level or temporary access.",
            "Tier", audience="owner"),
    Feature("logs", "📜", "Logs & Find person", "Owner → Logs (voice, members, moderation…) or Find person.", "OwnerLogs",
            audience="owner"),
    Feature("guardian", "🛡️", "Guardian & privacy doctor", "Owner → Guardian; /server doctor; /permissions privacy.",
            "Guardian", audience="owner", enabled=lambda b: _cfg(b, "guardian.enabled")),
    Feature("automod", "🚫", "AutoMod (words & invites)", "Owner → System → AutoMod.", "AutoMod", audience="owner",
            enabled=lambda b: bool(getattr(b.get_cog("AutoMod"), "cfg", {}).get("enabled")),
            setup="Off by default. Needs MESSAGE_CONTENT_INTENT=true."),
    Feature("backups", "💾", "Backups & downtime reports", "Owner → System.", "System", audience="owner",
            enabled=lambda b: _cfg(b, "system.backups_enabled")),
    Feature("updates", "⚙️", "Updates", "Owner → Updates.", "Updates", audience="owner",
            setup="Self-hosted installs: scripts/update.sh."),
]


def loaded(bot, f: Feature) -> bool:
    return f.cog is None or bot.get_cog(f.cog) is not None


def visible(bot, owner: bool) -> list[tuple[Feature, bool]]:
    """(feature, on) pairs a viewer should see. Members only see member features that are installed AND on;
    the owner sees everything installed, with its on/off state."""
    out = []
    for f in FEATURES:
        if not loaded(bot, f):
            continue
        try:
            on = bool(f.enabled(bot))
        except Exception:  # noqa: BLE001
            on = False
        if owner:
            out.append((f, on))
        elif f.audience == "member" and on:
            out.append((f, on))
    return out


def help_lines(bot, owner: bool) -> list[str]:
    lines = []
    for f, on in visible(bot, owner):
        state = "" if on else " · *off on this server*"
        lines.append(f"{f.emoji} **{f.name}**{state} — {f.help}")
    return lines


def status_lines(bot) -> list[str]:
    """Owner diagnostics: every module with installed / on / problem state."""
    lines = []
    for f in FEATURES:
        if not loaded(bot, f):
            failed = (bot.module_status or {}).get((f.cog or "").lower(), "")
            lines.append(f"🔴 {f.emoji} {f.name}: not loaded{' — ' + failed if failed.startswith('failed') else ''}")
            continue
        try:
            on = bool(f.enabled(bot))
        except Exception:  # noqa: BLE001
            on = False
        lines.append(f"{'🟢' if on else '⚪'} {f.emoji} {f.name}: {'on' if on else 'off'}"
                     + ("" if on or not f.setup else f" — {f.setup}"))
    return lines
