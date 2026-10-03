"""Control Center — the bot as a tap-first app inside Discord.

Home → section → actions → back. Navigation is private (ephemeral) and edited in place, so channels never fill with
bot messages; only social things (tonight's plan, teams) are posted publicly.

Every button / menu is a DynamicItem whose custom_id encodes the action ("va:<action>:<arg>", "vs:…" for menus,
"vu:…" for member pickers), so ALL of them keep working after a restart — including old ephemeral menus.
Handlers call the same services as the slash commands (music.control, VoiceRooms.do, Guests.create_invite,
Tier.set_tier, Access.grant …) — no duplicated business logic. What a member sees is permission-aware, and every
action is re-authorised server-side when it runs.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands

from .. import musicdb
from ..authz import Level
from ..db import LogQuery, iso
from ..render import owner_log_line
from ..ui import COLORS
from .music import STATIONS, MusicError, _esc, _fmt_ms, requester_of

log = logging.getLogger("vrbot.app")
S, P, G, R = discord.ButtonStyle.secondary, discord.ButtonStyle.primary, discord.ButtonStyle.success, discord.ButtonStyle.danger
NAV_RATE = 0.5


class Toast(Exception):
    """A friendly message for the person who tapped (never a technical error)."""


# ---------------------------------------------------------------- restart-proof components
class AppButton(discord.ui.DynamicItem[discord.ui.Button], template=r"va:(?P<a>[a-z0-9_]+):(?P<x>[^ ]*)"):
    def __init__(self, a: str, x: str = "", *, label: str | None = None, emoji: str | None = None, style=S,
                 row: int | None = None, disabled: bool = False):
        super().__init__(discord.ui.Button(label=label, emoji=emoji, style=style, row=row, disabled=disabled,
                                           custom_id=f"va:{a}:{x}"))
        self.a, self.x = a, x

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["a"], match["x"])

    async def callback(self, interaction: discord.Interaction):
        await interaction.client.get_cog("ControlCenter").dispatch(interaction, self.a, self.x)


class AppSelect(discord.ui.DynamicItem[discord.ui.Select], template=r"vs:(?P<a>[a-z0-9_]+):(?P<x>[^ ]*)"):
    def __init__(self, a: str, x: str = "", *, options: list[discord.SelectOption] | None = None,
                 placeholder: str | None = None, row: int | None = None):
        super().__init__(discord.ui.Select(custom_id=f"vs:{a}:{x}", placeholder=placeholder, row=row,
                                           options=options or [discord.SelectOption(label="—", value="-")]))
        self.a, self.x = a, x

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["a"], match["x"])

    async def callback(self, interaction: discord.Interaction):
        await interaction.client.get_cog("ControlCenter").dispatch(interaction, self.a, self.x,
                                                                   (interaction.data or {}).get("values", []))


class AppUserSelect(discord.ui.DynamicItem[discord.ui.UserSelect], template=r"vu:(?P<a>[a-z0-9_]+):(?P<x>[^ ]*)"):
    def __init__(self, a: str, x: str = "", *, placeholder: str | None = None, row: int | None = None):
        super().__init__(discord.ui.UserSelect(custom_id=f"vu:{a}:{x}", placeholder=placeholder, row=row))
        self.a, self.x = a, x

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["a"], match["x"])

    async def callback(self, interaction: discord.Interaction):
        await interaction.client.get_cog("ControlCenter").dispatch(interaction, self.a, self.x,
                                                                   (interaction.data or {}).get("values", []))


def view(*items) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    for it in items:
        if it is not None:
            v.add_item(it)
    return v


def back(to: str = "home", row: int = 4) -> AppButton:
    """Back = previous screen of THIS person's trail (`to` is only the fallback after a restart)."""
    return AppButton("back", to, label="Back", emoji="◀️", row=row)


def home(row: int = 4) -> AppButton:
    return AppButton("go", "home", label="Home", emoji="🏠", row=row)


def link(label: str, emoji: str, url: str, row: int) -> discord.ui.Button:
    return discord.ui.Button(label=label, emoji=emoji, url=url, row=row)


def home_buttons(owner: bool, row: int = 0) -> list:
    items = [AppButton("go", "music", label="Music", emoji="🎵", style=P, row=row),
             AppButton("go", "room", label="My Room", emoji="🔊", style=P, row=row),
             AppButton("go", "social", label="Social", emoji="🎮", row=row + 1),
             AppButton("go", "friends", label="Friends", emoji="👥", row=row + 1),
             AppButton("go", "help", label="Help", emoji="❓", row=row + 2)]
    if owner:
        items.append(AppButton("go", "owner", label="Owner", emoji="🔐", style=G, row=row + 2))
    return items


class TextModal(discord.ui.Modal):
    """The only place members type: search, room name, room limit."""

    def __init__(self, title: str, label: str, on_submit, *, max_length: int = 100, placeholder: str | None = None):
        super().__init__(title=title, timeout=300)
        self.field = discord.ui.TextInput(label=label, max_length=max_length, placeholder=placeholder)
        self.add_item(self.field)
        self._cb = on_submit

    async def on_submit(self, interaction: discord.Interaction):
        await self._cb(interaction, str(self.field.value).strip())


class ControlCenter(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.last: dict[int, float] = {}
        self.search_cache: dict[int, list] = {}
        self.sessions: dict[int, list[str]] = {}   # per-user navigation trail
        self.pending: dict[int, tuple] = {}         # what to play once the person joins voice
        self._tr: dict[int, dict] = {}              # per-click trace (logged at INFO as "ui {...}")

    async def cog_load(self):
        self.bot.add_dynamic_items(AppButton, AppSelect, AppUserSelect)

    # ---------------------------------------------------------- navigation framework (one surface per user)
    # Every person has their own stack of screens ("music", "music_radio", "owner_member|123" …). Forward = push,
    # Back = pop (falls back to PARENT after a restart), Home = reset. Every tap edits the SAME private message
    # (UPDATE_MESSAGE); slow actions first show a loading state; problems appear as a banner on the same screen.
    PARENT = {"music": "home", "music_quick": "music", "music_recent": "music", "music_favs": "music",
              "music_popular": "music", "music_radio": "music", "music_queue": "music_player", "music_player": "music",
              "music_results": "music", "music_help": "music", "music_needvoice": "music", "music_favdel": "music_favs",
              "room_people": "room", "room_member": "room_people", "friends_pull": "friends", "friends_roominv": "friends",
              "friends_invite": "friends", "friends_access": "friends", "friends_myinv": "friends", "help_adv": "help",
              "owner_member": "owner", "owner_activity": "owner", "owner_logs": "owner", "owner_access": "owner",
              "owner_guard": "owner"}

    @property
    def g(self) -> discord.Guild:
        return self.bot.guild

    def cog(self, name):
        return self.bot.get_cog(name)

    def is_owner(self, m) -> bool:
        return bool(self.g) and self.bot.cfg.privacy.is_owner_like(m.id, self.g.owner_id)

    def is_sponsor(self, m) -> bool:
        guests = self.cog("Guests")
        return bool(guests and guests.sponsor_ok(m))

    def owned_room(self, m):
        vr = self.cog("VoiceRooms")
        for cid, owner in (vr.rooms.items() if vr else []):
            if owner == m.id and self.g.get_channel(cid):
                return self.g.get_channel(cid)
        return None

    def member(self, uid) -> discord.Member:
        m = self.g.get_member(int(uid)) if uid else None
        if m is None:
            raise Toast("That person isn't in the server any more.")
        return m

    def need_owner(self, m):
        if not self.is_owner(m):
            raise Toast("You don't have access to that.")

    @staticmethod
    def _is_ephemeral_msg(i: discord.Interaction) -> bool:
        return bool(i.message and i.message.flags.ephemeral)

    def current(self, uid: int) -> str:
        st = self.sessions.get(uid) or ["home"]
        return st[-1]

    def _push(self, uid: int, ref: str):
        st = self.sessions.setdefault(uid, ["home"])
        if ref == "home":
            self.sessions[uid] = ["home"]
        elif st[-1] != ref:
            if ref in st:                      # going "up" to a screen already in the trail: cut back to it
                del st[st.index(ref) + 1:]
            else:
                st.append(ref)
        if len(st) > 12:
            del st[1:-11]

    async def _render(self, m, ref: str):
        name, *args = ref.split("|")
        return await getattr(self, f"s_{name}")(m, *args)

    async def _send(self, i: discord.Interaction, emb, v):
        kw = {"embed": emb, "view": v, "allowed_mentions": discord.AllowedMentions.none()}
        if i.response.is_done():
            await i.edit_original_response(**kw)
            return "edit_original"
        if self._is_ephemeral_msg(i) or (i.type == discord.InteractionType.modal_submit and i.message is not None
                                         and i.message.flags.ephemeral):
            await i.response.edit_message(**kw)
            return "update_message"
        await i.response.send_message(ephemeral=True, **kw)  # first tap on the public entry message
        return "new_private_surface"

    async def show(self, i: discord.Interaction, screen: str, *args, note: str | None = None):
        ref = "|".join([screen, *[str(a) for a in args]])
        if not (self._is_ephemeral_msg(i) or i.response.is_done() or i.type == discord.InteractionType.modal_submit):
            self.sessions[i.user.id] = ["home"]   # a fresh surface starts a fresh trail
        emb, v = await self._render(i.user, ref)
        if note:
            emb.description = f"{note}\n\n{emb.description or ''}".strip()
        self._push(i.user.id, ref)
        self._tr.setdefault(i.user.id, {})["resp"] = await self._send(i, emb, v)
        self._tr.setdefault(i.user.id, {})["next"] = ref

    async def loading(self, i: discord.Interaction, text: str):
        """Immediate visible feedback: the same message turns into a loading state with no buttons."""
        emb = discord.Embed(description=f"⏳ {text}", color=COLORS["BRAND"])
        if i.response.is_done():
            return
        if self._is_ephemeral_msg(i):
            await i.response.edit_message(embed=emb, view=None)
        elif i.type == discord.InteractionType.modal_submit and i.message is not None and i.message.flags.ephemeral:
            await i.response.edit_message(embed=emb, view=None)
        else:
            await i.response.send_message(embed=emb, ephemeral=True)

    async def open(self, i: discord.Interaction, screen: str = "home"):
        """Entry point used by /menu, /help, the welcome panel and the player message."""
        self.sessions[i.user.id] = ["home"]
        try:
            await self.show(i, screen)
        except Toast as e:
            await i.response.send_message(f"⚠️ {e}", ephemeral=True)

    async def dispatch(self, i: discord.Interaction, a: str, x: str, values: list | None = None):
        t0 = time.monotonic()
        before = self.current(i.user.id)
        self._tr[i.user.id] = {"user": i.user.id, "screen": before, "action": f"{a}:{x}", "next": None, "resp": None, "error": None}
        if t0 - self.last.get(i.user.id, 0) < NAV_RATE and not i.response.is_done():
            await i.response.defer()                 # double tap: acknowledge silently, no popup spam
            self._tr.setdefault(i.user.id, {})["error"] = "rate"
        else:
            self.last[i.user.id] = t0
            handler = getattr(self, f"h_{a}", None)
            try:
                if handler is None:
                    raise Toast("That button is from an older menu — here's the current one.")
                await handler(i, x, values or [])
            except (Toast, MusicError, app_commands.CheckFailure) as e:
                self._tr.setdefault(i.user.id, {})["error"] = str(e)[:80]
                await self.banner(i, f"⚠️ {e}")
            except discord.Forbidden:
                self._tr.setdefault(i.user.id, {})["error"] = "forbidden"
                await self.banner(i, "⚠️ You don't have access to that action.")
            except Exception as e:  # noqa: BLE001
                log.exception("control center action %s:%s", a, x)
                self._tr.setdefault(i.user.id, {})["error"] = type(e).__name__
                await self.banner(i, "⚠️ Something went wrong — please try again.")
        self._tr.setdefault(i.user.id, {})["ms"] = int((time.monotonic() - t0) * 1000)
        log.info("ui %s", self._tr.pop(i.user.id, {}))

    async def banner(self, i: discord.Interaction, text: str):
        """Show a problem ON the current screen (no extra popup)."""
        try:
            await self.show(i, *self.current(i.user.id).split("|"), note=text)
        except Exception:  # noqa: BLE001
            if i.response.is_done():
                await i.followup.send(text, ephemeral=True)
            else:
                await i.response.send_message(text, ephemeral=True)

    # ---- navigation handlers
    async def h_go(self, i, x, v):
        if x.startswith("owner"):
            self.need_owner(i.user)
        await self.show(i, x)

    async def h_back(self, i, x, v):
        st = self.sessions.get(i.user.id) or []
        if len(st) >= 2:
            st.pop()
            target = st.pop()
        else:
            target = self.PARENT.get(self.current(i.user.id).split("|")[0], x or "home")
        await self.show(i, *target.split("|"))

    # ================================================================== screens
    async def s_home(self, m):
        emb = discord.Embed(title=f"🎛️ {self.bot.cfg.identity.control_center_name}", color=COLORS["BRAND"],
                            description="Pick what you want to do.\n" + self._now_line())
        return emb, view(*home_buttons(self.is_owner(m)))

    # ---------------------------------------------------------- MUSIC (the reference app)
    def _music_state(self):
        """stopped | playing | paused, and radio vs normal track."""
        mu = self.cog("Music")
        p = mu.player() if mu else None
        cur = getattr(p, "current", None) if p else None
        if not cur:
            return mu, p, "stopped", False
        return mu, p, ("paused" if p.paused else "playing"), bool(getattr(p, "station", None) or cur.is_stream)

    def _now_line(self) -> str:
        mu, p, state, radio = self._music_state()
        if state == "stopped":
            return "Nothing playing"
        t = p.current
        if getattr(p, "station", None):
            e, n, _, _ = STATIONS[p.station]
            title = f"{e} {n} · live radio"
        else:
            title = f"{_esc(t.title)[:70]}"
        return f"{'⏸️ Paused' if state == 'paused' else '▶️ Now playing'}: **{title}**"

    async def s_music(self, m):
        mu, p, state, radio = self._music_state()
        emb = discord.Embed(title="🎵 Music", description=self._now_line(), color=COLORS["MUSIC"])
        playing = state != "stopped"
        return emb, view(
            AppButton("go", "music_player", label="Player", emoji="🎛️", style=P, row=0) if playing else None,
            AppButton("go", "music_quick", label="Quick Play", emoji="▶️", style=G, row=0),
            AppButton("go", "music_radio", label="Radio", emoji="📻", row=0),
            AppButton("go", "music_recent", label="Recent", emoji="🕘", row=1),
            AppButton("go", "music_favs", label="Favourites", emoji="❤️", row=1),
            AppButton("go", "music_popular", label="Popular", emoji="🔥", row=1),
            AppButton("msearch", "", label="Search", emoji="🔍", row=2),
            AppButton("go", "music_queue", label="Queue", emoji="📜", row=2) if playing and not radio else None,
            AppButton("go", "music_help", label="Help", emoji="❓", row=2),
            back(row=3), home(row=3))

    async def s_music_quick(self, m):
        emb = discord.Embed(title="▶️ Quick Play", description="What do you want?", color=COLORS["MUSIC"])
        return emb, view(AppButton("mood", "chill", label="Chill", emoji="😌", row=0),
                         AppButton("mood", "gaming", label="Gaming", emoji="🎮", row=0),
                         AppButton("mood", "party", label="Party", emoji="🔥", row=0),
                         AppButton("mood", "late", label="Late Night", emoji="🌙", row=1),
                         AppButton("mood", "surprise", label="Surprise Me", emoji="🎲", row=1),
                         back(row=2))

    async def s_music_player(self, m):
        mu, p, state, radio = self._music_state()
        if state == "stopped":
            emb = discord.Embed(title="🎵 Player", description="Nothing playing.", color=COLORS["MUSIC"])
            return emb, view(AppButton("go", "music_quick", label="Quick Play", emoji="▶️", style=G, row=0),
                             AppButton("go", "music_radio", label="Radio", emoji="📻", row=0), back(row=1), home(row=1))
        emb = mu.player_embed(p)
        emb.title = ("⏸️ Paused — " if state == "paused" else "") + (emb.title or "")
        pause = AppButton("mp", "pause_toggle", label="Resume" if state == "paused" else "Pause",
                          emoji="▶️" if state == "paused" else "⏸️", style=P, row=0)
        if radio:
            items = [pause, AppButton("mp", "stop", label="Stop", emoji="⏹️", style=R, row=0),
                     AppButton("go", "music_radio", label="Change station", emoji="📻", row=1)]
        else:
            q = list(p.queue)
            items = [AppButton("mp", "previous", emoji="⏮️", row=0), pause,
                     AppButton("mp", "skip", label="Next", emoji="⏭️", row=0),
                     AppButton("mp", "stop", label="Stop", emoji="⏹️", style=R, row=0),
                     AppButton("msave", "", label="Save", emoji="❤️", row=1),
                     AppButton("go", "music_queue", label=f"Queue ({len(q)})", emoji="📜", row=1),
                     AppButton("mp", "shuffle", emoji="🔀", row=1, disabled=len(q) < 2),
                     AppButton("mp", "loop", emoji="🔁", row=1)]
        return emb, view(*items, back(row=2), home(row=2))

    def _empty(self, title: str):
        emb = discord.Embed(title=title, description="Nothing here yet.", color=COLORS["MUSIC"])
        return emb, view(AppButton("go", "music_quick", label="Quick Play", emoji="▶️", style=G, row=0),
                         AppButton("go", "music_radio", label="Radio", emoji="📻", row=0),
                         AppButton("msearch", "", label="Search", emoji="🔍", row=0), back(row=1))

    @staticmethod
    def _track_options(rows: list[dict], emoji: str, seen: set) -> list[discord.SelectOption]:
        out = []
        for r in rows:
            uri = r.get("uri") or ""
            if not uri or len(uri) > 100 or uri in seen:
                continue
            seen.add(uri)
            out.append(discord.SelectOption(label=(r.get("label") or r["title"])[:100], value=uri, emoji=emoji,
                                            description=(r.get("author") or "")[:100] or None))
        return out

    async def _list_screen(self, title, rows, emoji, extra=None):
        opts = self._track_options(rows, emoji, set())[:25]
        if not opts:
            return self._empty(title)
        emb = discord.Embed(title=title, description="Pick one to play it.", color=COLORS["MUSIC"])
        return emb, view(AppSelect("playuri", "", options=opts, placeholder="▶️ Choose a song", row=0), *(extra or []), back(row=2))

    async def s_music_recent(self, m):
        return await self._list_screen("🕘 Recent", await musicdb.recent(self.bot.db, self.g.id, 25), "🕘")

    async def s_music_popular(self, m):
        return await self._list_screen("🔥 Popular", await musicdb.frequent(self.bot.db, self.g.id, 25), "🔥")

    async def s_music_favs(self, m):
        mine = await musicdb.favorites(self.bot.db, self.g.id, m.id)
        server = await musicdb.favorites(self.bot.db, self.g.id, 0)
        extra = [AppButton("go", "music_favdel", label="Remove one", emoji="🗑️", row=1) if mine else None]
        return await self._list_screen("❤️ Favourites", server + mine, "❤️", extra)

    async def s_music_favdel(self, m):
        opts = self._track_options(await musicdb.favorites(self.bot.db, self.g.id, m.id), "🗑️", set())[:25]
        if not opts:
            return self._empty("🗑️ Remove a favourite")
        emb = discord.Embed(title="🗑️ Remove a favourite", description="Pick the one to remove.", color=COLORS["MUSIC"])
        return emb, view(AppSelect("favdel", "", options=opts, placeholder="Remove…", row=0), back(row=1))

    async def s_music_radio(self, m):
        mu, p, state, radio = self._music_state()
        cur = getattr(p, "station", None) if p else None
        st247 = await self.bot.db.kv_get("music:247")
        opts = [discord.SelectOption(label=n, value=k, emoji=e, description=d[:100], default=k == cur)
                for k, (e, n, _, d) in STATIONS.items()]
        desc = "Choose a station — it starts right away."
        if cur:
            desc = f"On air: **{STATIONS[cur][0]} {STATIONS[cur][1]}**" + (" · 24/7" if st247 else "")
        emb = discord.Embed(title="📻 Radio", description=desc, color=COLORS["MUSIC"])
        return emb, view(AppSelect("station", "", options=opts, placeholder="📻 Choose a station", row=0),
                         AppButton("st247", "on" if not st247 else "off", label="24/7: off" if not st247 else "24/7: on",
                                   emoji="🔁", style=G if st247 else S, row=1, disabled=not cur),
                         AppButton("go", "music_player", label="Player", emoji="🎛️", row=1) if cur else None,
                         back(row=2))

    async def s_music_results(self, m):
        res = self.search_cache.get(m.id) or []
        if not res:
            return self._empty("🔍 No results")
        opts = [discord.SelectOption(label=(getattr(t, "name", None) or t.title)[:100], value=str(n),
                                     emoji="📃" if hasattr(t, "tracks") else "🎵",
                                     description=(getattr(t, "author", "") or ("playlist" if hasattr(t, "tracks") else ""))[:100] or None)
                for n, t in enumerate(res[:10])]
        emb = discord.Embed(title="🔍 Results", description="Pick one to play it.", color=COLORS["MUSIC"])
        return emb, view(AppSelect("pick", "", options=opts, placeholder="▶️ Choose", row=0),
                         AppButton("msearch", "", label="Search again", emoji="🔍", row=1), back(row=1))

    async def s_music_queue(self, m):
        mu, p, state, radio = self._music_state()
        if state == "stopped" or radio:
            emb = discord.Embed(title="📜 Queue", description="Nothing queued.", color=COLORS["MUSIC"])
            return emb, view(AppButton("go", "music_quick", label="Quick Play", emoji="▶️", style=G, row=0), back(row=1))
        q = list(p.queue)
        lines = [f"▶️ **{_esc(p.current.title)[:70]}**"]
        for n, t in enumerate(q[:10], 1):
            who = requester_of(t)
            lines.append(f"{n}. {_esc(t.title)[:60]} `{_fmt_ms(t.length)}`" + (f" · <@{who}>" if who else ""))
        if len(q) > 10:
            lines.append(f"…and {len(q) - 10} more")
        emb = discord.Embed(title="📜 Queue", description="\n".join(lines), color=COLORS["MUSIC"])
        if q:
            emb.set_footer(text=f"{len(q)} next · about {_fmt_ms(sum(t.length or 0 for t in q))}")
        opts = [discord.SelectOption(label=f"{n}. {t.title}"[:100], value=str(n - 1)) for n, t in enumerate(q[:25], 1)]
        return emb, view(AppSelect("qrm", "", options=opts, placeholder="🗑️ Remove a song", row=0) if opts else None,
                         AppButton("mq", "skip", label="Next", emoji="⏭️", row=1),
                         AppButton("mq", "shuffle", label="Shuffle", emoji="🔀", row=1, disabled=len(q) < 2),
                         AppButton("mq", "clear", label="Clear", emoji="🧹", style=R, row=1, disabled=not q),
                         back(row=2))

    async def s_music_needvoice(self, m):
        lobby = await self.bot.db.kv_get("channels:lobby")
        hub = await self.bot.db.kv_get("voicerooms:create_channel_id")
        base = f"https://discord.com/channels/{self.g.id}"
        emb = discord.Embed(title="🔊 Join a voice channel", color=COLORS["MUSIC"],
                            description="Music plays in the voice channel you're in. Join one, then tap **Try again**.")
        return emb, view(link("Lobby", "👋", f"{base}/{lobby}", 0) if lobby else None,
                         link("Create Room", "➕", f"{base}/{hub}", 0) if hub else None,
                         AppButton("retry", "", label="Try again", emoji="🔄", style=G, row=1), back(row=1))

    async def s_music_help(self, m):
        emb = discord.Embed(title="❓ Music help", color=COLORS["MUSIC"], description=(
            "• Join a voice channel first — music plays where you are.\n"
            "• **Quick Play** picks a mood · **Radio** is live and never ends.\n"
            "• **❤️ Save** a song while it plays; find it in **Favourites**.\n"
            "• Anyone listening can pause or skip; stopping is for whoever started it or the room owner."))
        return emb, view(back(row=0))

    # ---- music handlers
    async def _play(self, i, label: str, action):
        """Shared flow: not in voice → 'join voice' screen (remembers what you picked); else loading → play → Player."""
        if not (i.user.voice and i.user.voice.channel):
            self.pending[i.user.id] = (label, action)
            await self.show(i, "music_needvoice")
            return
        await self.loading(i, f"Starting {label}…")
        await action()
        await asyncio.sleep(1.2)                 # let playback actually start before showing the state
        await self.show(i, "music_player")

    async def h_retry(self, i, x, v):
        pend = self.pending.get(i.user.id)
        if not pend:
            await self.show(i, "music")
            return
        if not (i.user.voice and i.user.voice.channel):
            raise Toast("You're not in a voice channel yet.")
        self.pending.pop(i.user.id, None)
        st = self.sessions.get(i.user.id) or []
        if st and st[-1] == "music_needvoice":
            st.pop()
        await self._play(i, *pend)

    async def h_mood(self, i, x, v):
        names = {"chill": "Chill", "gaming": "Gaming", "party": "Party", "late": "Late Night", "surprise": "something"}
        user = i.user
        await self._play(i, names.get(x, x), lambda: self.cog("Music").play_something(user, None, x))

    async def h_station(self, i, x, v):
        key = v[0]
        user = i.user
        await self._play(i, STATIONS[key][1], lambda: self.cog("Music").start_station(user, None, key, continuous=False))

    async def h_st247(self, i, x, v):
        mu = self.cog("Music")
        p = mu.player()
        if x == "on":
            key = getattr(p, "station", None) if p else None
            if not key:
                raise Toast("Pick a station first.")
            user = i.user
            await self._play(i, f"{STATIONS[key][1]} (24/7)", lambda: mu.start_station(user, None, key, continuous=True))
        else:
            await self.bot.db.kv_set("music:247", None)
            if p is not None:
                p.inactive_timeout = self.bot.cfg.music.idle_disconnect_seconds
            await self.show(i, "music_radio", note="24/7 is off — the radio stops when everyone leaves.")

    async def h_playuri(self, i, x, v):
        uri, user = v[0], i.user
        await self._play(i, "your song", lambda: self.cog("Music").play_uri(user, None, uri))

    async def h_pick(self, i, x, v):
        res = self.search_cache.get(i.user.id) or []
        idx = int(v[0]) if v and v[0].isdigit() else -1
        if not 0 <= idx < len(res):
            raise Toast("Those results expired — search again.")
        item, user = res[idx], i.user
        await self._play(i, getattr(item, "name", None) or item.title, lambda: self.cog("Music").enqueue(user, None, item))

    async def h_mp(self, i, x, v):
        """Player buttons: act, then show the new state on the same screen."""
        mu = self.cog("Music")
        if x in ("skip", "previous", "stop"):
            await self.loading(i, {"skip": "Next song…", "previous": "Going back…", "stop": "Stopping…"}[x])
            msg = await mu.control(i.user, x)
            await asyncio.sleep(1.0)
            await self.show(i, "music" if x == "stop" else "music_player", note=msg if x == "stop" else None)
            return
        await mu.control(i.user, x)
        await self.show(i, "music_player")

    async def h_mq(self, i, x, v):
        msg = await self.cog("Music").control(i.user, x)
        if x == "skip":
            await asyncio.sleep(1.0)
        await self.show(i, "music_queue", note=msg)

    async def h_qrm(self, i, x, v):
        msg = await self.cog("Music").control(i.user, "remove", int(v[0]))
        await self.show(i, "music_queue", note=msg)

    async def h_msave(self, i, x, v):
        await self.show(i, "music_player", note=await self.cog("Music").save_current(i.user))

    async def h_favdel(self, i, x, v):
        for f in await musicdb.favorites(self.bot.db, self.g.id, i.user.id):
            if f["uri"] == v[0]:
                await musicdb.remove_favorite(self.bot.db, self.g.id, i.user.id, f["track_key"])
        await self.show(i, "music_favs", note="🗑️ Removed.")

    async def h_msearch(self, i, x, v):
        async def go(mi: discord.Interaction, text: str):
            self.last.pop(mi.user.id, None)
            await self.loading(mi, f"Searching “{text[:40]}”…")
            try:
                self.search_cache[mi.user.id] = await self.cog("Music").search(text, 8) if text else []
            except MusicError as e:
                self.search_cache[mi.user.id] = []
                await self.show(mi, "music", note=f"⚠️ {e}")
                return
            await self.show(mi, "music_results")
        await i.response.send_modal(TextModal("🔍 Find music", "Song, artist, playlist or link", go,
                                              placeholder="e.g. lofi hip hop, Daft Punk, a YouTube link"))

    # ---------------------------------------------------------- room
    async def s_room(self, m):
        ch = self.owned_room(m)
        hub_id = await self.bot.db.kv_get("voicerooms:create_channel_id")
        if not ch:
            in_voice = bool(m.voice and m.voice.channel)
            emb = discord.Embed(title="🔊 My Room", color=COLORS["VOICE"], description=(
                "You don't have a room right now.\n\n" + ("Tap **Create** — you'll be moved into your own room."
                                                          if in_voice else "Join **➕ Create Room** and your room appears instantly.")))
            return emb, view(AppButton("roomnew", "", label="Create my room", emoji="➕", style=G, row=0) if in_voice else None,
                             link("Create Room", "➕", f"https://discord.com/channels/{self.g.id}/{hub_id}", 0)
                             if hub_id and not in_voice else None, back(row=1))
        ev = ch.overwrites_for(self.g.default_role)
        locked, hidden = ev.connect is False, ev.view_channel is False
        people = [x for x in ch.members if not x.bot]
        emb = discord.Embed(title=f"🔊 {ch.name}", color=COLORS["VOICE"], description=(
            f"{len(people)} inside · {'🔒 locked' if locked else '🔓 open'} · {'🙈 hidden' if hidden else 'visible'}"
            f" · limit {ch.user_limit or 'none'}"))
        return emb, view(
            AppButton("ra", "unlock" if locked else "lock", label="Unlock" if locked else "Lock", emoji="🔓" if locked else "🔒", row=0),
            AppButton("ra", "show" if hidden else "hide", label="Show" if hidden else "Hide", emoji="👁️" if hidden else "🙈", row=0),
            AppButton("rrename", "", label="Rename", emoji="✏️", row=0),
            AppButton("rlimit", "", label="Limit", emoji="👥", row=0),
            AppButton("go", "room_people", label="People", emoji="🧑‍🤝‍🧑", style=P, row=1),
            AppButton("ginvopts", "", label="Invite Friend", emoji="🎟️", style=G, row=1) if self.is_sponsor(m) else None,
            link("Join", "🔊", ch.jump_url, 1),
            AppButton("rclose", "", label="Close", emoji="🗑️", style=R, row=1), back(row=2))

    async def s_room_people(self, m):
        if not self.owned_room(m):
            raise Toast("You don't have a room right now.")
        emb = discord.Embed(title="🧑‍🤝‍🧑 People", color=COLORS["VOICE"], description="Pick someone, then choose what to do.")
        return emb, view(AppUserSelect("rpick", "", placeholder="👤 Pick someone", row=0), back("room", row=1))

    async def s_room_member(self, m, uid):
        ch = self.owned_room(m)
        if not ch:
            raise Toast("You don't have a room right now.")
        t = self.member(uid)
        meta = self.cog("VoiceRooms").meta.get(ch.id, {})
        state = ("✅ trusted" if t.id in meta.get("trusted", []) else "🚫 blocked" if t.id in meta.get("blocked", []) else "—")
        inside = bool(t.voice and t.voice.channel == ch)
        emb = discord.Embed(title=f"👤 {t.display_name}", color=COLORS["VOICE"],
                            description=f"In your room: {'yes' if inside else 'no'} · {state}")
        u = t.id
        return emb, view(AppButton("rm", f"trust.{u}", label="Trust", emoji="✅", style=G, row=0),
                         AppButton("rm", f"untrust.{u}", label="Untrust", emoji="➖", row=0),
                         AppButton("rm", f"invite.{u}", label="Invite", emoji="📨", style=P, row=0),
                         AppButton("rm", f"block.{u}", label="Block", emoji="🚫", style=R, row=1),
                         AppButton("rm", f"unblock.{u}", label="Unblock", emoji="♻️", row=1),
                         AppButton("rm", f"disconnect.{u}", label="Disconnect", emoji="👋", row=1, disabled=not inside),
                         AppButton("rm", f"transfer.{u}", label="Make owner", emoji="👑", row=1, disabled=not inside),
                         back("room_people", row=2))

    # ---------------------------------------------------------- social
    async def s_social(self, m):
        emb = discord.Embed(title="🎮 Social", color=COLORS["FUN"], description=(
            "🌙 **Tonight** — post a quick \"what are we up for?\" plan everyone can tap\n"
            "🎲 **Teams** — random teams from the people in your voice channel"
            + ("" if m.voice and m.voice.channel else "\n\n*Teams need you to be in a voice channel.*")))
        return emb, view(AppButton("tonight", "", label="Tonight's plan", emoji="🌙", style=P, row=0),
                         AppButton("teams", "2", label="2 teams", emoji="🎲", row=1),
                         AppButton("teams", "3", label="3 teams", emoji="🎲", row=1),
                         AppButton("teams", "1", label="Pick one", emoji="👆", row=1),
                         AppButton("teams", "0", label="Random order", emoji="🔢", row=1), back(row=2))

    # ---------------------------------------------------------- friends
    async def s_friends(self, m):
        sponsor = self.is_sponsor(m)
        room = self.owned_room(m)
        lines = ["🧲 **Bring someone in** — pull a friend into your voice channel"]
        if room:
            lines.append("📨 **Invite to my room** — let someone in, even when it's locked")
        if sponsor:
            lines += ["🎟️ **Invite a friend** — a private one-time link for someone new",
                      "✅ **Give access** — normal access for someone who has none"]
        emb = discord.Embed(title="👥 Friends", color=COLORS["BRAND"], description="\n".join(lines))
        return emb, view(AppButton("go", "friends_pull", label="Bring someone in", emoji="🧲", row=0),
                         AppButton("go", "friends_roominv", label="Invite to my room", emoji="📨", row=0) if room else None,
                         AppButton("ginvopts", "", label="Invite a friend", emoji="🎟️", style=G, row=1) if sponsor else None,
                         AppButton("go", "friends_access", label="Give access", emoji="✅", row=1) if sponsor else None,
                         AppButton("go", "friends_myinv", label="My invites", emoji="📋", row=1) if sponsor else None,
                         back(row=2))

    async def s_friends_pull(self, m):
        emb = discord.Embed(title="🧲 Bring someone in", color=COLORS["BRAND"],
                            description="Pick a friend who's in voice — they'll be moved to your voice channel.")
        return emb, view(AppUserSelect("pull", "", placeholder="👤 Who?", row=0), back("friends", row=1))

    async def s_friends_roominv(self, m):
        emb = discord.Embed(title="📨 Invite to my room", color=COLORS["BRAND"], description="They get a private link and can join even when locked.")
        return emb, view(AppUserSelect("roominv", "", placeholder="👤 Who?", row=0), back("friends", row=1))

    async def s_friends_invite(self, m):
        if not self.is_sponsor(m):
            raise Toast("You don't have access to that.")
        room = self.owned_room(m)
        where = f"straight into **{room.name}**" if room and m.voice and m.voice.channel == room else "into the server"
        emb = discord.Embed(title="🎟️ Invite a friend", color=COLORS["OK"], description=(
            f"I'll make a private link that brings them {where}. They get normal member access.\nHow should it work?"))
        return emb, view(AppButton("ginv", "1.24", label="1 person · 24 h", emoji="👤", style=G, row=0),
                         AppButton("ginv", "5.24", label="Up to 5 · 24 h", emoji="👥", row=0),
                         AppButton("ginv", "1.72", label="1 person · 3 days", emoji="📅", row=0), back("friends", row=1))

    async def s_friends_access(self, m):
        emb = discord.Embed(title="✅ Give access", color=COLORS["OK"],
                            description="For someone who joined but has no access yet. Gives normal member access only.")
        return emb, view(AppUserSelect("gacc", "", placeholder="👤 Who?", row=0), back("friends", row=1))

    async def s_friends_myinv(self, m):
        guests = self.cog("Guests")
        reg = await guests._prune(await guests.registry())
        mine = [(c, v) for c, v in reg.items() if v["sponsor"] == m.id]
        lines = [f"`{c}` · {v['uses']}× · ends <t:{int(v['expires'])}:R>" for c, v in mine] or ["No open invites."]
        emb = discord.Embed(title="📋 My invites", color=COLORS["BRAND"], description="\n".join(lines))
        return emb, view(AppButton("gcancel", "", label="Cancel all", emoji="🧹", style=R, row=0) if mine else None,
                         back("friends", row=1))

    # ---------------------------------------------------------- help
    async def s_help(self, m):
        emb = discord.Embed(title="❓ How it works", color=COLORS["GUIDE"], description=(
            f"Everything works with taps — open the **🎛️ {self.bot.cfg.identity.control_center_name}** menu or use the buttons below.\n\n"
            "🎵 **Want music?** Join a voice channel → Music → Play something.\n"
            "🔊 **Want your own room?** Join ➕ Create Room, or My Room → Create.\n"
            "🔒 **Keep it private?** My Room → Lock or Hide.\n"
            "👥 **Bring a friend in?** Friends → Bring someone in.\n"
            "🌙 **Planning tonight?** Social → Tonight's plan.\n"
            "🎲 **Picking teams?** Social → 2 teams.\n\n"
            "Inside your room there's also a control panel in the room's chat."))
        return emb, view(*home_buttons(False, 0), AppButton("go", "help_adv", label="Advanced", emoji="⌨️", row=1), back(row=1))

    async def s_help_adv(self, m):
        emb = discord.Embed(title="⌨️ Advanced (optional)", color=COLORS["GUIDE"], description=(
            "Prefer typing? These do the same as the buttons:\n"
            "`/music play <song or link>` · `/music skip` · `/music queue`\n"
            "`/voice lock` · `/voice rename` · `/voice trust @someone`\n"
            "`/tonight` · `/teams` · `/permissions why`\n"
            "Right-click someone → **Apps → Pull into my voice**"))
        return emb, view(back("help", row=0))

    # ---------------------------------------------------------- owner
    async def s_owner(self, m):
        self.need_owner(m)
        lines = await self.cog("Server").status_lines(m)
        text = "\n".join(lines)
        emb = discord.Embed(title="🔐 Owner", color=COLORS["INFO"], description=text[:3900])
        emb.set_footer(text="Only you see this")
        return emb, view(AppUserSelect("opick", "", placeholder="👤 Manage a member…", row=0),
                         AppButton("go", "owner_logs", label="Logs", emoji="📜", row=1),
                         AppButton("go", "owner_access", label="Temporary access", emoji="🔑", row=1),
                         AppButton("go", "owner_guard", label="Guardian", emoji="🛡️", row=1),
                         AppButton("go", "owner", label="Refresh", emoji="🔄", row=1), back(row=2))

    async def s_owner_member(self, m, uid):
        self.need_owner(m)
        t = self.member(uid)
        tier = self.cog("Tier")
        emb = tier.card(t)
        emb.color = COLORS["INFO"]
        access = self.cog("Access")
        active = [g for g in access.grants.values() if g["user_id"] == t.id]
        if active:
            emb.add_field(name="Temporary access", value="\n".join(
                f"{'Admin' if g['kind'] == 'admin' else 'Moderator'} until <t:{int(g['expires'])}:t>" for g in active), inline=False)
        roles = tier.roles()
        names = {k: (self.g.get_role(v).name if self.g.get_role(v) else k) for k, v in roles.items()}
        owner_acc = self.is_owner(t) or t.bot
        u = t.id
        return emb, view(
            AppButton("ot", f"tier1.{u}", label=f"Tier 1 · {names.get('tier1')}", row=0, disabled=owner_acc),
            AppButton("ot", f"tier2.{u}", label=f"Tier 2 · {names.get('tier2')}", row=0, disabled=owner_acc),
            AppButton("ot", f"tier3.{u}", label=f"Tier 3 · {names.get('tier3')}", row=0, disabled=owner_acc),
            AppButton("oe", f"mod.{u}", label="Temp Mod 30m", emoji="⏱️", row=1, disabled=t.bot),
            AppButton("oe", f"admin.{u}", label="Temp Admin 10m", emoji="⚡", style=R, row=1, disabled=t.bot),
            AppButton("orv", str(u), label="Revoke", emoji="⛔", row=1, disabled=not active),
            AppButton("oact", str(u), label="Activity", emoji="📜", row=2), back("owner", row=2))

    async def s_owner_activity(self, m, uid):
        self.need_owner(m)
        t = self.member(uid)
        rows = await self.bot.db.query_events(LogQuery(guild_id=self.g.id, user_id=t.id, limit=15))
        lines = [owner_log_line(r) for r in rows] or ["Nothing recorded."]
        emb = discord.Embed(title=f"📜 {t.display_name} · recent activity", color=COLORS["INFO"], description="\n".join(lines)[:3900])
        return emb, view(AppButton("opickback", str(t.id), label="Back", emoji="◀️", row=0))

    LOG_CATS = {"voice": ("🎙️", "Voice", {"category": "voice"}), "members": ("👤", "Members", {"category": "membership"}),
                "invites": ("🎟️", "Invites", {"types": ["invite_used", "guest_join", "guest_invite_create", "standard_access"]}),
                "moderation": ("🛡️", "Moderation", {"category": "moderation"}),
                "server": ("⚙️", "Server changes", {"category": "structure"}), "security": ("🔐", "Security", {"category": "security"})}
    RANGES = {"today": "Today", "24h": "24 h", "7d": "7 days"}

    async def s_owner_logs(self, m, cat: str = "voice", rng: str = "24h", uid: str = "0"):
        self.need_owner(m)
        now = datetime.now(timezone.utc)
        since = {"today": now.replace(hour=0, minute=0, second=0, microsecond=0), "24h": now - timedelta(hours=24),
                 "7d": now - timedelta(days=7)}[rng]
        emoji, name, filt = self.LOG_CATS[cat]
        q = LogQuery(guild_id=self.g.id, since=iso(since), limit=15, user_id=int(uid) or None, **filt)
        rows = await self.bot.db.query_events(q)
        who = f" · {self.g.get_member(int(uid)).display_name}" if int(uid) and self.g.get_member(int(uid)) else ""
        emb = discord.Embed(title=f"📜 {emoji} {name} · {self.RANGES[rng]}{who}", color=COLORS["INFO"],
                            description="\n".join(owner_log_line(r) for r in rows)[:3900] or "Nothing in this period.")
        emb.set_footer(text="Newest first · up to 15 · /logs for more")
        opts = [discord.SelectOption(label=n, emoji=e, value=k, default=k == cat) for k, (e, n, _) in self.LOG_CATS.items()]
        return emb, view(AppSelect("olc", f"{rng}.{uid}", options=opts, row=0),
                         AppUserSelect("olm", f"{cat}.{rng}", placeholder="👤 Only this member…", row=1),
                         *[AppButton("olr", f"{cat}.{k}.{uid}", label=v, style=P if k == rng else S, row=2) for k, v in self.RANGES.items()],
                         AppButton("olr", f"{cat}.{rng}.0", label="Everyone", row=2) if int(uid) else None, back("owner", row=3))

    async def s_owner_access(self, m):
        self.need_owner(m)
        lines = self.cog("Access").status_lines() or ["Nobody has temporary access."]
        emb = discord.Embed(title="🔑 Temporary access", color=COLORS["INFO"], description="\n".join(lines) +
                            "\n\nTo give access, pick a member below (Temp Mod / Temp Admin).")
        return emb, view(AppUserSelect("opick", "", placeholder="👤 Pick a member…", row=0), back("owner", row=1))

    async def s_owner_guard(self, m):
        self.need_owner(m)
        rows = await self.bot.db.query_events(LogQuery(guild_id=self.g.id, types=["guardian_alert"], limit=8))
        import json as _j
        alerts = []
        for r in rows:
            d = _j.loads(r.get("details") or "{}")
            alerts.append(f"<t:{int(datetime.fromisoformat(r['ts']).timestamp())}:R> {d.get('severity', '')} {d.get('title', '')}"[:200])
        sm = "🛑 ON (read-only)" if self.bot.safe_mode() else "off"
        emb = discord.Embed(title="🛡️ Guardian", color=COLORS["INFO"], description=(
            f"**Safe Mode:** {sm} · **Auto-heal:** {'on' if self.bot.cfg.autoheal.enabled else 'off'}\n\n**Latest alerts**\n"
            + ("\n".join(alerts) or "None.")))
        return emb, view(AppButton("go", "owner_guard", label="Refresh", emoji="🔄", row=0), back("owner", row=0))

    # ================================================================== handlers
    # room
    def _room_or_toast(self, m):
        ch = self.owned_room(m)
        if not ch:
            raise Toast("You don't have a room right now.")
        return ch

    async def h_roomnew(self, i, x, v):
        vr = self.cog("VoiceRooms")
        if not (i.user.voice and i.user.voice.channel):
            raise Toast("Join a voice channel first.")
        hub = await vr.hub()
        if hub is None:
            raise Toast("Rooms are switched off right now.")
        await self.busy(i)
        room = await vr._create_for(i.user, hub)
        if room is None:
            raise Toast("Couldn't make a room just now — wait a moment and try again.")
        await self.show(i, "room", note=f"✅ Your room is ready: {room.mention}")

    async def h_ra(self, i, x, v):
        ch = self._room_or_toast(i.user)
        msg = await self.cog("VoiceRooms").do(ch, x, None, actor=i.user)
        await self.show(i, "room", note=msg)

    async def h_rrename(self, i, x, v):
        ch = self._room_or_toast(i.user)

        async def go(mi, text):
            try:
                msg = await self.cog("VoiceRooms").do(ch, "rename", text, actor=mi.user)
                await self.show(mi, "room", note=msg)
            except Exception as e:  # noqa: BLE001
                await self.toast(mi, f"⚠️ {e}")
        await i.response.send_modal(TextModal("✏️ Rename your room", "New name", go, max_length=60))

    async def h_rlimit(self, i, x, v):
        ch = self._room_or_toast(i.user)

        async def go(mi, text):
            if not text.isdigit() or int(text) > 99:
                await self.toast(mi, "Enter a number 0–99 (0 = no limit).")
                return
            msg = await self.cog("VoiceRooms").do(ch, "limit", int(text), actor=mi.user)
            await self.show(mi, "room", note=msg)
        await i.response.send_modal(TextModal("👥 Room limit", "Max people (0 = no limit)", go, max_length=2))

    async def h_rclose(self, i, x, v):
        await self.cog("VoiceRooms").ask_close(i, self._room_or_toast(i.user))

    async def h_rpick(self, i, x, v):
        await self.show(i, "room_member", v[0])

    async def h_rm(self, i, x, v):
        action, uid = x.split(".", 1)
        ch = self._room_or_toast(i.user)
        msg = await self.cog("VoiceRooms").do(ch, action, self.member(uid), actor=i.user)
        if action == "transfer":
            await self.show(i, "room_people", note=msg) if self.owned_room(i.user) else await self.show(i, "home", note=msg)
        else:
            await self.show(i, "room_member", uid, note=msg)

    # social
    async def h_tonight(self, i, x, v):
        cid = await self.bot.db.kv_get("channels:chat") or await self.bot.db.kv_get("channels:welcome")
        ch = self.g.get_channel(int(cid)) if cid else None
        if ch is None:
            raise Toast("The server isn't set up yet — ask the owner to run /setup.")
        msg, new = await self.cog("Fun").post_tonight(ch, i.user)
        await self.show(i, "social", note=(f"🌙 Plan posted in {ch.mention} — {msg.jump_url}" if new
                                           else f"🌙 There's already a plan for tonight — {msg.jump_url}"))

    async def h_teams(self, i, x, v):
        vc = getattr(i.user.voice, "channel", None)
        if not vc:
            raise Toast("Join a voice channel first — teams are made from the people in it.")
        if len([m for m in vc.members if not m.bot]) < 2:
            raise Toast("You need at least 2 people in your voice channel.")
        n = int(x)
        emb = self.cog("Fun").teams_embed(vc, n)
        msg = await vc.send(embed=emb, view=view(AppButton("reroll", f"{n}.{i.user.id}", label="Reroll", emoji="🔁")),
                            allowed_mentions=discord.AllowedMentions.none())
        await self.show(i, "social", note=f"🎲 Posted in {vc.mention}'s chat — {msg.jump_url}")

    async def h_reroll(self, i, x, v):
        n, creator = x.split(".")
        vc = getattr(i.user.voice, "channel", None)
        if i.user.id != int(creator) or not vc:
            raise Toast("Only the person who made the teams can reroll (from voice).")
        await i.response.edit_message(embed=self.cog("Fun").teams_embed(vc, int(n)))

    # friends
    async def h_pull(self, i, x, v):
        dest = getattr(i.user.voice, "channel", None)
        if not dest:
            raise Toast("Join a voice channel first.")
        msg = await self.cog("VoiceRooms").move_service(i.user, self.member(v[0]), dest)
        await self.show(i, "friends", note=msg)

    async def h_roominv(self, i, x, v):
        ch = self._room_or_toast(i.user)
        msg = await self.cog("VoiceRooms").do(ch, "invite", self.member(v[0]), actor=i.user)
        await self.show(i, "friends", note=msg)

    async def h_ginvopts(self, i, x, v):
        await self.show(i, "friends_invite")

    async def h_ginv(self, i, x, v):
        uses, hours = (int(n) for n in x.split("."))
        room = self.owned_room(i.user)
        if room and not (i.user.voice and i.user.voice.channel == room):
            room = None
        await self.busy(i)
        ok, msg = await self.cog("Guests").create_invite(i.user, room=room, uses=uses, hours=hours)
        await self.show(i, "friends", note=msg)

    async def h_gacc(self, i, x, v):
        msg = await self.cog("Guests").give_access(i.user, self.member(v[0]))
        await self.show(i, "friends", note=msg)

    async def h_gcancel(self, i, x, v):
        guests = self.cog("Guests")
        reg = await guests.registry()
        n = 0
        for inv in await self.g.invites():
            if reg.get(inv.code, {}).get("sponsor") == i.user.id:
                await inv.delete(reason=f"[bot] cancelled by {i.user.display_name}")
                reg.pop(inv.code, None)
                n += 1
        await self.bot.db.kv_set("guests:invites", reg)
        await self.show(i, "friends_myinv", note=f"🧹 Cancelled {n} invite(s).")

    # owner
    async def h_opick(self, i, x, v):
        self.need_owner(i.user)
        await self.show(i, "owner_member", v[0])

    async def h_opickback(self, i, x, v):
        self.need_owner(i.user)
        await self.show(i, "owner_member", x)

    async def h_ot(self, i, x, v):
        self.need_owner(i.user)
        tier, uid = x.split(".")
        await self.busy(i)
        msg = await self.cog("Tier").set_tier(self.member(uid), tier, i.user, "owner control center")
        await self.show(i, "owner_member", uid, note=msg)

    async def h_oe(self, i, x, v):
        self.need_owner(i.user)
        kind, uid = x.split(".")
        await self.busy(i)
        msg = await self.cog("Access").grant(self.member(uid), kind, None, i.user, "owner control center")
        await self.show(i, "owner_member", uid, note=msg)

    async def h_orv(self, i, x, v):
        self.need_owner(i.user)
        access = self.cog("Access")
        kinds = [g["kind"] for g in access.grants.values() if g["user_id"] == int(x)]
        await self.busy(i)
        msgs = [await access.revoke(int(x), k, "manual revoke (control center)", i.user) for k in kinds] or ["Nothing to revoke."]
        await self.show(i, "owner_member", x, note="\n".join(msgs))

    async def h_oact(self, i, x, v):
        self.need_owner(i.user)
        await self.show(i, "owner_activity", x)

    async def h_olc(self, i, x, v):
        self.need_owner(i.user)
        rng, uid = x.split(".")
        await self.show(i, "owner_logs", v[0], rng, uid)

    async def h_olr(self, i, x, v):
        self.need_owner(i.user)
        await self.show(i, "owner_logs", *x.split("."))

    async def h_olm(self, i, x, v):
        self.need_owner(i.user)
        cat, rng = x.split(".")
        await self.show(i, "owner_logs", cat, rng, v[0])

    # ---------------------------------------------------------- public entry points
    def public_view(self) -> discord.ui.View:
        """The persistent Control Center message (same for everyone; owner tools appear after the first tap)."""
        return view(*home_buttons(False, 0))

    def public_embed(self) -> discord.Embed:
        ident = self.bot.cfg.identity
        emb = discord.Embed(title=f"🎛️ {ident.control_center_name}", color=COLORS["BRAND"],
                            description=(ident.subtitle + "\n" if ident.subtitle else "") + "Pick what you want to do.")
        emb.set_footer(text="Menus open privately — only you see yours")
        return emb

    @app_commands.command(name="menu", description="Open the menu (music, your room, friends, help)")
    async def menu(self, interaction: discord.Interaction):
        await self.open(interaction, "home")


async def setup(bot):
    await bot.add_cog(ControlCenter(bot))
