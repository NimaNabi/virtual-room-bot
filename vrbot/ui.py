"""Shared Discord UI helpers: confirmation buttons, chunked embeds, formatting."""
from __future__ import annotations

from datetime import datetime
from typing import Awaitable, Callable

import discord

# One colour system for every embed (tasteful, dark-mode friendly). Status colours for owner tools; mood colours for
# member-facing features. Never reused as "rank" colours.
COLORS = {"CRITICAL": 0xE74C3C, "WARNING": 0xF1C40F, "INFO": 0x3498DB, "OK": 0x2ECC71, "BRAND": 0x5865F2,
          "ERROR": 0xE74C3C, "MUSIC": 0xEC4899, "VOICE": 0x22B8CF, "FUN": 0xF59E0B, "GUIDE": 0x5865F2}
SEV_ICON = {"CRITICAL": "🔴", "WARNING": "🟡", "INFO": "🔵"}


def chunk_lines(lines: list[str], limit: int = 3900) -> list[str]:
    pages, cur = [], ""
    for line in lines:
        if len(line) > limit:
            line = line[: limit - 1] + "…"
        if len(cur) + len(line) + 1 > limit:
            pages.append(cur)
            cur = ""
        cur += line + "\n"
    if cur or not pages:
        pages.append(cur or "(nothing)")
    return pages


async def send_pages(interaction: discord.Interaction, title: str, lines: list[str], *, color: int = COLORS["BRAND"],
                     ephemeral: bool = True, max_pages: int = 5, file: discord.File | None = None) -> None:
    pages = chunk_lines(lines)
    shown = pages[:max_pages]
    embeds = [discord.Embed(title=title if i == 0 else f"{title} (cont.)", description=p, color=color)
              for i, p in enumerate(shown)]
    if len(pages) > max_pages:
        embeds[-1].set_footer(text=f"…{len(pages) - max_pages} more page(s) truncated. Use /logs export for everything.")
    send = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
    kwargs = {"embeds": embeds[:10], "ephemeral": ephemeral, "allowed_mentions": discord.AllowedMentions.none()}
    if file:
        kwargs["file"] = file
    await send(**kwargs)


def ts_fmt(iso: str | None, style: str = "f") -> str:
    if not iso:
        return "?"
    try:
        return discord.utils.format_dt(datetime.fromisoformat(iso), style)
    except ValueError:
        return iso


class ConfirmView(discord.ui.View):
    """Buttons only the invoking user (or an allowed checker) may press."""

    def __init__(self, author_id: int, on_confirm: Callable[[discord.Interaction], Awaitable[None]],
                 confirm_label: str = "Apply", danger: bool = True, timeout: float = 180,
                 allowed: Callable[[discord.Interaction], bool] | None = None):
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.on_confirm = on_confirm
        self.allowed = allowed
        self.done = False
        self.confirm.label = confirm_label
        self.confirm.style = discord.ButtonStyle.danger if danger else discord.ButtonStyle.success

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        ok = self.allowed(interaction) if self.allowed else interaction.user.id == self.author_id
        if not ok:
            await interaction.response.send_message("This confirmation is not for you.", ephemeral=True)
        return ok

    @discord.ui.button(label="Apply", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.done:
            return
        self.done = True
        for c in self.children:
            c.disabled = True
        await interaction.response.edit_message(view=self)
        try:
            await self.on_confirm(interaction)
        except discord.app_commands.CheckFailure as e:  # safe mode / rate limit / authorization
            await interaction.followup.send(f"⚠️ {e}", ephemeral=True)
        except discord.Forbidden as e:
            await interaction.followup.send(f"⚠️ Discord refused: {e.text or e}", ephemeral=True)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.done = True
        for c in self.children:
            c.disabled = True
        await interaction.response.edit_message(content="Cancelled. Nothing was changed.", view=self)
        self.stop()


def who(name: str | None, uid: int | None) -> str:
    if uid is None and not name:
        return "unknown"
    return f"{name or '?'}" + (f" (`{uid}`)" if uid else "")
