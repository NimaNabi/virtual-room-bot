"""Curated info content: ONE welcome panel, ONE rules post and the Control Center — bot messages
edited in place. Published by the setup wizard; message IDs live in kv (onboarding:panel / onboarding:rules /
onboarding:guide) and channel IDs in kv channels:*. Nothing here mentions trust levels.
"""
from __future__ import annotations

import logging

import discord
from discord.ext import commands


from ..ui import COLORS

log = logging.getLogger("vrbot.onboarding")
FALLBACK = {"welcome": None, "chat": None, "lobby": None, "music": None, "rules": None, "guide": None}



async def channel_id(bot, key: str) -> int | None:
    v = await bot.db.kv_get(f"channels:{key}")
    return int(v) if v else FALLBACK.get(key)


class HelpButtonView(discord.ui.View):
    """Welcome panel buttons: links + private quick guide (custom_id kept from v1 so old panels still answer)."""

    def __init__(self, bot=None, ids: dict | None = None):
        super().__init__(timeout=None)
        g = bot.guild if bot else None
        if g and ids:
            base = f"https://discord.com/channels/{g.id}"
            self.add_item(discord.ui.Button(label="Chat", emoji="💬", url=f"{base}/{ids['chat']}", row=0))
            self.add_item(discord.ui.Button(label="Lobby", emoji="🔊", url=f"{base}/{ids['lobby']}", row=0))
            if ids.get("hub"):
                self.add_item(discord.ui.Button(label="Create a room", emoji="➕", url=f"{base}/{ids['hub']}", row=0))
            if ids.get("guide"):
                self.add_item(discord.ui.Button(label="Menu", emoji="🎛️", url=f"{base}/{ids['guide']}", row=0))

    @discord.ui.button(label="Quick tour", emoji="❓", style=discord.ButtonStyle.secondary, custom_id="ob:guide", row=1)
    async def guide(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.client.get_cog("ControlCenter").open(interaction, "home")


class Onboarding(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        self.bot.add_view(HelpButtonView())  # persistent handler for the quick-tour button

    async def ids(self) -> dict:
        out = {k: await channel_id(self.bot, k) for k in FALLBACK}
        out["hub"] = await self.bot.db.kv_get("voicerooms:create_channel_id")
        return out

    def welcome_embed(self, ids: dict) -> discord.Embed:
        g = self.bot.guild
        hub = f"<#{ids['hub']}>" if ids.get("hub") else "**➕ Create Room**"
        emb = discord.Embed(title=f"Welcome to {g.name} 👋", color=COLORS["BRAND"], description=(
            "Here's how this server works:\n\n"
            f"💬 **Chat** — <#{ids['chat']}>\n"
            f"🔊 **Hang out** — drop into <#{ids['lobby']}>\n"
            f"➕ **Your own room** — join {hub}; you get a private room you control\n"
            "🎵 **Music** — join a voice channel, then tap 🎛️ **Menu → Music**\n"
            f"📜 **Rules** — <#{ids['rules']}> \n"
            + (f"🎛️ **Menu** — <#{ids['guide']}>: music, your room, friends — all by tapping\n" if ids.get("guide") else "")
            + "🙋 **Stuck?** tap **❓ Quick tour** below or message the server owner"))
        emb.set_footer(text=self.bot.cfg.identity.control_center_name)
        return emb

    def rules_embed(self) -> discord.Embed:
        emb = discord.Embed(title="📜 Rules", color=COLORS["BRAND"])
        for emoji, title, text in self.bot.cfg.onboarding.rules:
            emb.add_field(name=f"{emoji} {title}", value=text, inline=False)
        emb.set_footer(text="The server owner has the final say.")
        return emb

    async def _upsert(self, key: str, channel_id_: int, embeds: list[discord.Embed], view=None, pin: bool = False) -> dict:
        ch = self.bot.guild.get_channel(channel_id_)
        if ch is None:
            return {"error": f"channel {channel_id_} missing"}
        mid = await self.bot.db.kv_get(f"onboarding:{key}")
        if mid:
            try:
                msg = await ch.fetch_message(mid)
                await msg.edit(embeds=embeds, view=view)
                return {"message": msg.id, "action": "updated", "jump": msg.jump_url}
            except discord.NotFound:
                pass
        self.bot.guard(f"onboarding_{key}", None)
        msg = await ch.send(embeds=embeds, view=view, allowed_mentions=discord.AllowedMentions.none())
        if pin:
            try:
                await msg.pin(reason=f"bot: {key}")
                async for m in ch.history(limit=5):  # remove Discord's "pinned a message" notice (ours)
                    if m.type == discord.MessageType.pins_add and m.author.id == self.bot.user.id:
                        await m.delete()
            except discord.HTTPException:
                log.warning("could not pin %s", key)
        await self.bot.db.kv_set(f"onboarding:{key}", msg.id)
        return {"message": msg.id, "action": "created" + ("+pinned" if pin else ""), "jump": msg.jump_url}

    async def publish(self) -> dict:
        ids = await self.ids()
        out = {"panel": await self._upsert("panel", ids["welcome"], [self.welcome_embed(ids)], HelpButtonView(self.bot, ids), pin=True),
               "rules": await self._upsert("rules", ids["rules"], [self.rules_embed()])}
        if ids.get("guide"):  # the bot guide IS the Control Center: one persistent message, everything by tapping
            app = self.bot.get_cog("ControlCenter")
            out["guide"] = await self._upsert("guide", ids["guide"], [app.public_embed()], app.public_view(), pin=True)
        return out


async def setup(bot):
    await bot.add_cog(Onboarding(bot))
