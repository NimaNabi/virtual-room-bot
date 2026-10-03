"""Member-facing guide: ONE source for /help, the menu post and the welcome panel's guide button.

Never mentions trust levels, owner tools or moderation. Short sections, examples, buttons to switch sections.
"""
from __future__ import annotations

import discord

from .ui import COLORS

SECTIONS = {
    "music": ("🎵", "Music", COLORS["MUSIC"], (
        "Join any voice channel, then:\n"
        "`/music play <song or link>` — YouTube, SoundCloud, playlists\n"
        "`/music pause` · `/music resume` · `/music skip` · `/music previous`\n"
        "`/music queue` · `/music nowplaying` · `/music shuffle` · `/music loop`\n"
        "`/music stop` clears the queue · `/music disconnect` sends the bot away\n\n"
        "💡 Works great inside your own room — everyone in the room shares the queue.")),
    "rooms": ("🔊", "Your own voice room", COLORS["VOICE"], (
        "Join **{hub}** — you get your own room instantly and you're its owner.\n"
        "A control panel appears in the room's chat:\n"
        "🔒 **Lock / Unlock** — nobody new can join · 👁️ **Hide / Show** — invisible to others\n"
        "✏️ **Rename** · 👥 **Limit** — max people\n"
        "The room disappears by itself when everyone leaves.")),
    "people": ("👥", "Invite & room controls", COLORS["VOICE"], (
        "In your room's panel, pick a member in the menu, then:\n"
        "✅ **Trust** — they can always join, even when locked · ➖ **Untrust**\n"
        "📨 **Invite** — sends them a link to your room\n"
        "🚫 **Block** / ♻️ **Unblock** — keep someone out of your room\n"
        "👋 **Disconnect** — remove them from your room\n"
        "👑 **Make owner** — hand the room over · 🙋 **Claim** — take over a room whose owner left\n"
        "Right-click a member → **Apps → Pull into my voice** to bring a friend into your room.")),
    "fun": ("🛠", "Useful extras", COLORS["FUN"], (
        "`/tonight` — ask who's up for gaming, music, a movie or chilling; people tap to join a plan\n"
        "`/teams` — random teams from the people in your voice channel (`teams:1` picks one person)\n"
        "`/permissions why` — explains why you can't see or join something\n"
        "`/help` — this guide, any time (only you see it)")),
}
ORDER = ["music", "rooms", "people", "fun"]


def section_embed(key: str, hub_mention: str = "➕ Create Room") -> discord.Embed:
    emoji, title, color, text = SECTIONS[key]
    return discord.Embed(title=f"{emoji} {title}", description=text.replace("{hub}", hub_mention), color=color)


def overview_embed(hub_mention: str = "➕ Create Room") -> discord.Embed:
    emb = discord.Embed(title="🤖 Help", color=COLORS["GUIDE"],
                        description="Everything is a slash command or a button. Pick a topic below.")
    for key in ORDER:
        emoji, title, _, text = SECTIONS[key]
        first = text.replace("{hub}", hub_mention).split("\n")[0]
        emb.add_field(name=f"{emoji} {title}", value=first, inline=False)
    return emb


class GuideView(discord.ui.View):
    """Persistent section buttons (fixed custom_ids). Answers are private to whoever clicks."""

    def __init__(self):
        super().__init__(timeout=None)
        for key in ORDER:
            emoji, title, _, _ = SECTIONS[key]
            b = discord.ui.Button(label=title, emoji=emoji, style=discord.ButtonStyle.secondary, custom_id=f"guide:{key}")
            b.callback = self._cb(key)
            self.add_item(b)

    def _cb(self, key):
        async def cb(interaction: discord.Interaction):
            hub = await interaction.client.db.kv_get("voicerooms:create_channel_id")
            await interaction.response.send_message(embed=section_embed(key, f"<#{hub}>" if hub else "➕ Create Room"),
                                                    view=GuideView(), ephemeral=True)
        return cb
