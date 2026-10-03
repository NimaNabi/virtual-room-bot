"""Two small social tools for a voice-first friend server.

/tonight — "what are we up for?" panel. Members tap Gaming / Music / Movie / Chill / Other; the panel shows who is
           in for what, live, and points at Create Room when a plan has people. One panel, buttons, no spam.
/teams   — split the people in YOUR voice channel into random teams (or pick one person). Reroll button.
"""
from __future__ import annotations

import random
import time

import discord
from discord import app_commands
from discord.ext import commands

from ..ui import COLORS

OPTIONS = [("gaming", "🎮", "Gaming"), ("music", "🎵", "Music"), ("movie", "🎬", "Movie"), ("chill", "🛋️", "Chill"),
           ("other", "✨", "Something else")]
TTL = 12 * 3600


def toggle(state: dict, option: str, user_id: int) -> dict:
    """One choice per person: tapping your current choice removes it, tapping another moves you."""
    picks = {k: [u for u in v if u != user_id] for k, v in state.get("picks", {}).items()}
    if user_id not in state.get("picks", {}).get(option, []):
        picks.setdefault(option, []).append(user_id)
    return {**state, "picks": picks}


def split_teams(members: list, n: int, rng: random.Random | None = None) -> list[list]:
    rng = rng or random.Random()
    pool = list(members)
    rng.shuffle(pool)
    n = max(1, min(n, len(pool) or 1))
    return [pool[i::n] for i in range(n)]


class TonightView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        for key, emoji, label in OPTIONS:
            b = discord.ui.Button(label=label, emoji=emoji, style=discord.ButtonStyle.secondary, custom_id=f"tn:{key}")
            b.callback = self._cb(key)
            self.add_item(b)

    def _cb(self, key):
        async def cb(interaction: discord.Interaction):
            await interaction.client.get_cog("Fun").vote(interaction, key)
        return cb


class Fun(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        self.bot.add_view(TonightView())

    async def embed(self, state: dict) -> discord.Embed:
        g = self.bot.guild
        lines = []
        top = None
        for key, emoji, label in OPTIONS:
            ids = state.get("picks", {}).get(key, [])
            names = ", ".join(g.get_member(u).display_name for u in ids if g.get_member(u)) or "—"
            lines.append(f"{emoji} **{label}** · {len(ids)}  \n{names}")
            if ids and (top is None or len(ids) > top[1]):
                top = (f"{emoji} {label}", len(ids))
        hub = await self.bot.db.kv_get("voicerooms:create_channel_id")
        foot = f"\n\n**Most picked:** {top[0]} → hop into <#{hub}> and make a room." if top and top[1] >= 2 and hub else ""
        emb = discord.Embed(title="🌙 What are we up for tonight?", description="\n".join(lines) + foot, color=COLORS["VOICE"])
        emb.set_footer(text=f"Tap to join a plan · tap again to leave · started by {state.get('by_name', '?')}")
        return emb

    async def vote(self, interaction: discord.Interaction, key: str):
        mid = interaction.message.id
        state = await self.bot.db.kv_get(f"tonight:{mid}")
        if not state or time.time() - state.get("ts", 0) > TTL:
            await interaction.response.send_message("This plan has ended — start a new one with `/tonight`.", ephemeral=True)
            return
        state = toggle(state, key, interaction.user.id)
        await self.bot.db.kv_set(f"tonight:{mid}", state)
        await interaction.response.edit_message(embed=await self.embed(state), allowed_mentions=discord.AllowedMentions.none())

    async def post_tonight(self, channel, member) -> tuple[discord.Message, bool]:
        """One live plan at a time: if a plan from the last 3 h exists, point to it instead of posting another."""
        last = await self.bot.db.kv_get("tonight:last")
        if last and time.time() - last["ts"] < 3 * 3600:
            ch = self.bot.guild.get_channel(last["channel"])
            try:
                return await ch.fetch_message(last["message"]), False
            except (discord.HTTPException, AttributeError):
                pass
        state = {"ts": time.time(), "by": member.id, "by_name": member.display_name, "picks": {}}
        msg = await channel.send(embed=await self.embed(state), view=TonightView(), allowed_mentions=discord.AllowedMentions.none())
        await self.bot.db.kv_set(f"tonight:{msg.id}", state)
        await self.bot.db.kv_set("tonight:last", {"ts": state["ts"], "channel": channel.id, "message": msg.id})
        return msg, True

    @app_commands.command(name="tonight", description="Ask who's up for gaming, music, a movie or chilling tonight")
    async def tonight(self, interaction: discord.Interaction):
        msg, new = await self.post_tonight(interaction.channel, interaction.user)
        await interaction.response.send_message("🌙 Plan posted." if new else f"🌙 There's already a plan tonight: {msg.jump_url}",
                                                ephemeral=True)

    @app_commands.command(name="teams", description="Random teams (or one random pick) from people in your voice channel")
    @app_commands.describe(teams="Number of teams (1 = pick one person)")
    async def teams(self, interaction: discord.Interaction, teams: app_commands.Range[int, 1, 6] = 2):
        vc = getattr(interaction.user.voice, "channel", None)
        if not vc:
            await interaction.response.send_message("Join a voice channel first — teams are made from the people in it.",
                                                    ephemeral=True)
            return
        await interaction.response.send_message(embed=self.teams_embed(vc, teams), view=RerollView(interaction.user.id, teams),
                                                allowed_mentions=discord.AllowedMentions.none())

    def teams_embed(self, vc, n: int) -> discord.Embed:
        people = [m.display_name for m in vc.members if not m.bot]
        if not people:
            return discord.Embed(description="Nobody to split.", color=COLORS["WARNING"])
        if n == 1:
            return discord.Embed(title="🎲 Random pick", description=f"**{random.choice(people)}**", color=COLORS["FUN"])
        if n == 0:  # random order
            order = random.sample(people, len(people))
            return discord.Embed(title="🎲 Random order", description=chr(10).join(f"{i}. {p}" for i, p in enumerate(order, 1)),
                                 color=COLORS["FUN"])
        emb = discord.Embed(title=f"🎲 Teams · {vc.name}", color=COLORS["FUN"])
        for i, team in enumerate(split_teams(people, n), 1):
            emb.add_field(name=f"Team {i}", value="\n".join(team) or "—", inline=True)
        return emb


class RerollView(discord.ui.View):
    def __init__(self, owner_id: int, n: int):
        super().__init__(timeout=900)
        self.owner_id, self.n = owner_id, n

    @discord.ui.button(label="Reroll", emoji="🔁", style=discord.ButtonStyle.secondary)
    async def reroll(self, interaction: discord.Interaction, button):
        vc = getattr(interaction.user.voice, "channel", None)
        if interaction.user.id != self.owner_id or not vc:
            await interaction.response.send_message("Only the person who made the teams can reroll (from voice).", ephemeral=True)
            return
        await interaction.response.edit_message(embed=interaction.client.get_cog("Fun").teams_embed(vc, self.n))


async def setup(bot):
    await bot.add_cog(Fun(bot))
