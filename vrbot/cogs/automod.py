"""Optional, simple content AutoMod: blocked words and invite links. OFF until the owner turns it on.

* Blocked words: whole-word, Unicode-aware matching; `*` = any letters ("bad*" also catches "badly", while "ass" never
  matches "class"). Text is normalised first: NFKC, case-folded, Arabic-script letter variants unified (ي/ى→ی, ك→ک,
  ة→ه), tatweel, zero-width joiners and diacritics removed — so look-alike spellings can't slip through.
* Invite filter: Discord invite links are removed unless they point to THIS server (optional allowance).
* Edited messages are checked again. Moderators/owners are always exempt; the most trusted level optionally too.
* Needs the privileged Message Content intent (MESSAGE_CONTENT_INTENT=true); without it AutoMod stays inactive and the
  owner screen says so. Settings live in the database (Owner → System → AutoMod), not in a config file.
* Removed messages are logged for the owner (author, channel, rule — the text only as a short excerpt).
"""
from __future__ import annotations

import asyncio
import logging
import re
import unicodedata

import discord
from discord.ext import commands

from ..authz import Level

log = logging.getLogger("vrbot.automod")
KV = "automod:cfg"
DEFAULTS = {"enabled": False, "words": [], "invites": False, "allow_own_invites": True, "exempt_top_level": False}
INVITE = re.compile(r"(?:https?://)?(?:www\.)?(?:discord(?:app)?\.com/invite|discord\.gg)/([A-Za-z0-9-]+)", re.I)
_MAP = str.maketrans({"ي": "ی", "ى": "ی", "ك": "ک", "ة": "ه", "ـ": None, "‌": None, "‍": None,
                      "​": None, "⁠": None})


def normalize(text: str) -> str:
    t = unicodedata.normalize("NFKC", text or "").casefold().translate(_MAP)
    # drop combining marks (Arabic harakat, Latin accents) so "naïve"/"naive" and vowelled spellings match
    return "".join(ch for ch in unicodedata.normalize("NFD", t) if not unicodedata.combining(ch))


def word_pattern(word: str) -> re.Pattern:
    w = normalize(word).strip()
    body = "".join(r"\w*" if ch == "*" else re.escape(ch) for ch in w)
    return re.compile(rf"(?<!\w){body}(?!\w)")


def blocked_word(text: str, words: list[str]) -> str | None:
    t = normalize(text)
    for w in words:
        if w.strip() and word_pattern(w).search(t):
            return w
    return None


def invite_codes(text: str) -> list[str]:
    return [m.group(1) for m in INVITE.finditer(text or "")]


def parse_words(raw: str) -> list[str]:
    """Owner input: one per line or comma-separated; duplicates and blanks dropped, order kept."""
    out: list[str] = []
    for part in re.split(r"[,\n]", raw or ""):
        w = part.strip()
        if w and w.lower() not in (x.lower() for x in out):
            out.append(w[:60])
    return out[:200]


class AutoMod(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.cfg = dict(DEFAULTS)

    async def cog_load(self):
        self.cfg = {**DEFAULTS, **(await self.bot.db.kv_get(KV) or {})}

    async def save(self, actor: discord.abc.User, **changes) -> dict:
        self.cfg = {**self.cfg, **changes}
        await self.bot.db.kv_set(KV, self.cfg)
        g = self.bot.guild
        if g:
            shown = {k: (len(v) if k == "words" else v) for k, v in changes.items()}  # never log the word list itself
            await self.bot.db.add_event(type="automod_config", category="security", guild_id=g.id, actor_id=actor.id,
                                        actor_name=actor.display_name, actor_confidence="confirmed",
                                        details={"changed": shown}, source="bot")
        return self.cfg

    @property
    def can_read(self) -> bool:
        return bool(self.bot.settings.message_content_intent)

    @property
    def active(self) -> bool:
        return self.cfg["enabled"] and self.can_read and bool(self.cfg["words"] or self.cfg["invites"])

    def exempt(self, m: discord.Member) -> bool:
        if m.bot or self.bot.level_of(m) >= Level.MOD or m.guild_permissions.manage_messages:
            return True
        if self.cfg.get("exempt_top_level"):
            from ..trust import current_tier, tier_role_ids
            tr = tier_role_ids(self.bot.cfg)
            return bool(tr) and current_tier([r.id for r in m.roles], tr) == "tier1"
        return False

    async def problem(self, msg: discord.Message) -> tuple[str, str] | None:
        w = blocked_word(msg.content, self.cfg["words"])
        if w:
            return "blocked word", w
        if self.cfg["invites"]:
            for code in invite_codes(msg.content):
                if self.cfg.get("allow_own_invites"):
                    try:
                        inv = await self.bot.fetch_invite(code, with_counts=False)
                        if inv.guild and inv.guild.id == msg.guild.id:
                            continue
                    except discord.HTTPException:
                        pass  # unknown/expired invite: treated as not-ours
                return "invite link to another server", code
        return None

    async def check(self, msg: discord.Message) -> bool:
        g = self.bot.guild
        if not self.active or not g or not msg.guild or msg.guild.id != g.id or not isinstance(msg.author, discord.Member):
            return False
        if self.exempt(msg.author):
            return False
        found = await self.problem(msg)
        if not found:
            return False
        rule, what = found
        try:
            await msg.delete()
        except discord.HTTPException:
            log.info("automod could not delete a message (missing Manage Messages?)")
            return False
        try:   # short notice that removes itself after a few seconds
            await msg.channel.send(f"⚠️ {msg.author.mention}, your message was removed ({rule}).",
                                   allowed_mentions=discord.AllowedMentions(users=[msg.author]), delete_after=6)
        except discord.HTTPException:
            pass
        await self.bot.db.add_event(type="automod_delete", category="moderation", guild_id=g.id, target_id=msg.author.id,
                                    target_name=msg.author.display_name, actor_name="AutoMod", actor_confidence="confirmed",
                                    channel_id=msg.channel.id, channel_name=getattr(msg.channel, "name", None),
                                    reason=rule, details={"excerpt": (msg.content or "")[:80], "rule": rule}, source="bot")
        return True

    @commands.Cog.listener()
    async def on_message(self, msg: discord.Message):
        if self.active and not msg.author.bot:
            await self.check(msg)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if self.active and not after.author.bot and before.content != after.content:
            await asyncio.sleep(0)   # let the edit event be logged first
            await self.check(after)

    def status_lines(self) -> list[str]:
        c = self.cfg
        lines = [f"**AutoMod:** {'🟢 on' if c['enabled'] else '⚪ off'}"
                 + ("" if self.can_read else " · ⚠️ inactive: needs MESSAGE_CONTENT_INTENT=true (and the intent on in the Developer Portal)"),
                 f"**Blocked words:** {len(c['words'])}",
                 f"**Invite links:** {'removed' if c['invites'] else 'allowed'}"
                 + (" (this server's own invites allowed)" if c['invites'] and c.get('allow_own_invites') else ""),
                 f"**Exempt:** owner, moderators{', most trusted level' if c.get('exempt_top_level') else ''}"]
        return lines


async def setup(bot):
    await bot.add_cog(AutoMod(bot))
