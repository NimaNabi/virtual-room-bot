"""Music — one service, three surfaces (slash commands, the live player message, the Control Center).

Playback runs through a separate Lavalink node (Wavelink). If it is down, only music reports "offline".
Voice audio is only SENT; nothing is recorded.

Control rules (predictable, anti-grief):
  * you must be in the bot's voice channel (the owner/mods may control from anywhere);
  * play / pause / skip / previous / shuffle / repeat / save: anyone listening;
  * stop, clear, volume, removing someone else's track, 24/7: the room owner, the person who started it,
    whoever is the only listener, or the owner;
  * one action per person every 1.2 s; queue changes are serialised with a lock (simultaneous clicks are safe).
24/7: live radio streams (verified to load through our Lavalink). Sleeps when the channel has nobody in it for
`music.idle_247_minutes`, wakes when someone joins, survives restarts, gives up after repeated stream failures.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

from .. import musicdb
from ..authz import Level
from ..ui import COLORS

log = logging.getLogger("vrbot.music")

try:
    import wavelink
except Exception:  # noqa: BLE001
    wavelink = None

# Live radio (SomaFM, listener-supported internet radio). Each URL was verified to load as a stream via Lavalink.
STATIONS = {
    "lofi": ("☕", "Lofi beats", "https://ice1.somafm.com/fluid-128-mp3", "SomaFM Fluid — instrumental hip-hop"),
    "chill": ("🌙", "Chill", "https://ice1.somafm.com/groovesalad-128-mp3", "SomaFM Groove Salad — downtempo"),
    "gaming": ("🎮", "Gaming", "https://ice1.somafm.com/defcon-128-mp3", "SomaFM DEF CON Radio — electronic"),
    "party": ("🔥", "Party", "https://ice1.somafm.com/poptron-128-mp3", "SomaFM PopTron — electro-pop"),
    "mixed": ("🎵", "Mixed", "https://ice1.somafm.com/indiepop-128-mp3", "SomaFM Indie Pop Rocks"),
    "late": ("🌌", "Late night", "https://ice1.somafm.com/beatblender-128-mp3", "SomaFM Beat Blender — deep house"),
    "drone": ("🛰️", "Drone Zone", "https://ice1.somafm.com/dronezone-128-mp3", "SomaFM — ambient space"),
    "secretagent": ("🕶️", "Secret Agent", "https://ice1.somafm.com/secretagent-128-mp3", "SomaFM — spy lounge"),
    "lush": ("🌸", "Lush", "https://ice1.somafm.com/lush-128-mp3", "SomaFM — soft vocals"),
    "deepspace": ("🌠", "Deep Space One", "https://ice1.somafm.com/deepspaceone-128-mp3", "SomaFM — deep ambient"),
    "spacestation": ("🚀", "Space Station", "https://ice1.somafm.com/spacestation-128-mp3", "SomaFM — spaced-out electronica"),
    "thetrip": ("🌀", "The Trip", "https://ice1.somafm.com/thetrip-128-mp3", "SomaFM — progressive house"),
    "u80s": ("📼", "Underground 80s", "https://ice1.somafm.com/u80s-128-mp3", "SomaFM — 80s new wave"),
    "vaporwaves": ("🌴", "Vaporwaves", "https://ice1.somafm.com/vaporwaves-128-mp3", "SomaFM — vaporwave"),
    "synphaera": ("✨", "Synphaera", "https://ice1.somafm.com/synphaera-128-mp3", "SomaFM — modern ambient"),
    "sonicuniverse": ("🎷", "Sonic Universe", "https://ice1.somafm.com/sonicuniverse-128-mp3", "SomaFM — jazz"),
    "bootliquor": ("🤠", "Boot Liquor", "https://ice1.somafm.com/bootliquor-128-mp3", "SomaFM — Americana"),
    "seventies": ("🕺", "Left Coast 70s", "https://ice1.somafm.com/seventies-128-mp3", "SomaFM — 70s"),
    "metal": ("🤘", "Metal Detector", "https://ice1.somafm.com/metal-128-mp3", "SomaFM — metal"),
}
MOODS = {"chill": "chill", "gaming": "gaming", "party": "party", "late": "drone"}
DESTRUCTIVE = {"stop", "clear", "volume", "disconnect", "station_stop"}
RATE_S = 1.2


class MusicError(app_commands.CheckFailure):
    """A friendly, member-facing reason (never a technical error)."""


def _fmt_ms(ms: int | None) -> str:
    if not ms:
        return "live"
    s = ms // 1000
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def _esc(t: str | None) -> str:
    return discord.utils.escape_markdown(t or "")


def requester_of(track) -> int | None:
    try:
        return int(dict(track.extras).get("requester") or 0) or None
    except Exception:  # noqa: BLE001
        return None


def may_control(action: str, *, is_listener: bool, is_staff: bool, is_room_owner: bool, is_starter: bool,
                only_listener: bool, owns_item: bool = False) -> bool:
    """Pure control policy (unit-tested)."""
    if is_staff:
        return True
    if not is_listener:
        return False
    if action == "remove":
        return owns_item or is_room_owner or only_listener
    if action in DESTRUCTIVE:
        return is_room_owner or is_starter or only_listener
    return True


# ---------------------------------------------------------------- the live player message (persistent buttons)
class PlayerView(discord.ui.View):
    def __init__(self, paused: bool = False):
        super().__init__(timeout=None)
        self.b_pause.emoji = "▶️" if paused else "⏸️"

    async def _act(self, interaction: discord.Interaction, action: str):
        await interaction.client.get_cog("Music").button(interaction, action)

    @discord.ui.button(emoji="⏮️", style=discord.ButtonStyle.secondary, custom_id="mu:previous", row=0)
    async def b_prev(self, i, b):
        await self._act(i, "previous")

    @discord.ui.button(emoji="⏸️", style=discord.ButtonStyle.primary, custom_id="mu:pause_toggle", row=0)
    async def b_pause(self, i, b):
        await self._act(i, "pause_toggle")

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary, custom_id="mu:skip", row=0)
    async def b_skip(self, i, b):
        await self._act(i, "skip")

    @discord.ui.button(emoji="⏹️", style=discord.ButtonStyle.danger, custom_id="mu:stop", row=0)
    async def b_stop(self, i, b):
        await self._act(i, "stop")

    @discord.ui.button(emoji="🔀", style=discord.ButtonStyle.secondary, custom_id="mu:shuffle", row=1)
    async def b_shuffle(self, i, b):
        await self._act(i, "shuffle")

    @discord.ui.button(emoji="🔁", style=discord.ButtonStyle.secondary, custom_id="mu:loop", row=1)
    async def b_loop(self, i, b):
        await self._act(i, "loop")

    @discord.ui.button(emoji="❤️", style=discord.ButtonStyle.secondary, custom_id="mu:save", row=1)
    async def b_save(self, i, b):
        await self._act(i, "save")

    @discord.ui.button(label="More", emoji="🎵", style=discord.ButtonStyle.secondary, custom_id="mu:more", row=1)
    async def b_more(self, i, b):
        app = i.client.get_cog("ControlCenter")
        await app.open(i, "music")


class Music(commands.GroupCog, group_name="music", group_description="Play music in voice"):
    def __init__(self, bot):
        self.bot = bot
        self.state = "disabled"
        self.lock = asyncio.Lock()
        self.last_action: dict[int, float] = {}
        self.panels: dict[int, int] = {}            # text channel id -> player message id
        self.empty_since: float | None = None
        self.stream_failures: list[float] = []
        super().__init__()

    async def cog_load(self):
        self.bot.add_view(PlayerView())
        s, cfg = self.bot.settings, self.bot.cfg.music
        if not cfg.enabled:
            self.state = "disabled in config"
            return
        if wavelink is None:
            self.state = "wavelink not installed"
            return
        if not s.lavalink_uri or not s.lavalink_password:
            self.state = "not configured (LAVALINK_URI/LAVALINK_PASSWORD)"
            return
        self.state = "connecting"
        self.bot.loop.create_task(self._connect())
        self.watch_247.start()

    def cog_unload(self):
        self.watch_247.cancel()

    async def _connect(self):
        await self.bot.wait_until_ready()
        try:
            node = wavelink.Node(uri=self.bot.settings.lavalink_uri, password=self.bot.settings.lavalink_password,
                                 identifier="local", retries=None)
            await wavelink.Pool.connect(nodes=[node], client=self.bot, cache_capacity=100)
        except Exception as e:  # noqa: BLE001
            self.state = f"offline ({type(e).__name__})"
            log.warning("Lavalink connect failed: %s", e)

    def status_text(self) -> str:
        if wavelink is None or self.state.startswith(("disabled", "not configured", "wavelink")):
            return self.state
        try:
            ok = wavelink.Pool.get_node().status == wavelink.NodeStatus.CONNECTED
        except Exception:  # noqa: BLE001
            ok = False
        players = [vc for vc in self.bot.voice_clients if getattr(vc, "playing", False)]
        return f"online; playing in {len(players)} channel(s)" if ok else f"offline ({self.state})"

    # ---------------------------------------------------------- service: state + checks
    def online(self) -> bool:
        try:
            return wavelink is not None and wavelink.Pool.get_node().status == wavelink.NodeStatus.CONNECTED
        except Exception:  # noqa: BLE001
            return False

    def _need_online(self):
        if not self.online():
            raise MusicError("Music is taking a break right now — try again in a minute.")

    def player(self):
        g = self.bot.guild
        return g.voice_client if g else None

    def _rate(self, member: discord.Member):
        now = time.time()
        if now - self.last_action.get(member.id, 0) < RATE_S:
            raise MusicError("Easy there — one tap at a time.")
        self.last_action[member.id] = now

    def _room_owner(self, channel) -> int | None:
        vr = self.bot.get_cog("VoiceRooms")
        return vr.rooms.get(channel.id) if vr and channel else None

    def check(self, member: discord.Member, action: str, *, item=None):
        p = self.player()
        if p is None or not p.channel:
            raise MusicError("Nothing is playing right now.")
        listeners = [m for m in p.channel.members if not m.bot]
        ok = may_control(action, is_listener=member in listeners, is_staff=self.bot.level_of(member) >= Level.MOD,
                         is_room_owner=self._room_owner(p.channel) == member.id,
                         is_starter=getattr(p, "starter_id", None) == member.id,
                         only_listener=listeners == [member],
                         owns_item=item is not None and requester_of(item) == member.id)
        if not ok:
            if member not in listeners:
                raise MusicError(f"Join {p.channel.mention} to control the music.")
            raise MusicError("Only the room owner or whoever started the music can do that.")
        return p

    async def ensure_player(self, member: discord.Member, home: discord.abc.Messageable | None):
        self._need_online()
        vs = member.voice
        if not vs or not vs.channel:
            raise MusicError("Join a voice channel first.")
        perms = vs.channel.permissions_for(member.guild.me)
        if not (perms.connect and perms.speak):
            raise MusicError("I can't join that voice channel.")
        p = member.guild.voice_client
        if p is None:
            p = await vs.channel.connect(cls=wavelink.Player, self_deaf=True)
            p.autoplay = wavelink.AutoPlayMode.partial
            p.inactive_timeout = self.bot.cfg.music.idle_disconnect_seconds
            await p.set_volume(self.bot.cfg.music.default_volume)
            p.starter_id = member.id
        elif p.channel != vs.channel:
            if p.playing and any(not m.bot for m in p.channel.members):
                raise MusicError(f"I'm already playing in {p.channel.mention}.")
            await p.move_to(vs.channel)
            p.starter_id = member.id
        if home is not None and getattr(home, "id", None) and not getattr(p, "home", None):
            p.home = home
        if getattr(p, "home", None) is None:
            p.home = vs.channel  # the voice channel's own text chat
        return p

    # ---------------------------------------------------------- service: search / queue
    async def search(self, query: str, limit: int = 5) -> list:
        self._need_online()
        source = None if query.startswith(("http://", "https://")) else self.bot.cfg.music.search_prefix
        try:
            res = await wavelink.Playable.search(query, source=source) if source else await wavelink.Playable.search(query)
        except Exception:  # noqa: BLE001
            log.warning("search failed for %r", query, exc_info=True)
            raise MusicError("Couldn't search right now — try again or try another song.") from None
        if not res:
            return []
        if isinstance(res, wavelink.Playlist):
            return [res]
        return list(res)[:limit]

    async def enqueue(self, member: discord.Member, home, item, *, announce: bool = True) -> str:
        """item: a Playable, a Playlist, a URL or a search text. Returns a friendly message."""
        self._rate(member)
        p = await self.ensure_player(member, home)
        if isinstance(item, str):
            found = await self.search(item, 1)
            if not found:
                raise MusicError("Nothing found for that.")
            item = found[0]
        async with self.lock:
            if len(p.queue) >= self.bot.cfg.music.max_queue:
                raise MusicError("The queue is full.")
            if getattr(p, "station", None):          # leaving radio mode for normal music
                await self._clear_247(p)
            if isinstance(item, wavelink.Playlist):
                for t in item.tracks:
                    t.extras = {"requester": member.id}
                added = await p.queue.put_wait(item)
                msg = f"➕ Added **{_esc(item.name)}** ({added} songs)."
            else:
                item.extras = {"requester": member.id}
                await p.queue.put_wait(item)
                msg = f"➕ Added **{_esc(item.title)}**" + (f" — {_esc(item.author)}" if item.author else "")
            if not p.playing:
                await p.play(p.queue.get())
                msg = msg.replace("➕ Added", "▶️ Playing", 1)
        return msg

    async def play_uri(self, member, home, uri: str) -> str:
        return await self.enqueue(member, home, uri)

    async def control(self, member: discord.Member, action: str, arg=None) -> str:
        """Every playback action (slash commands, player buttons and the Control Center all land here)."""
        self._rate(member)
        item = None
        if action == "remove":
            p0 = self.player()
            q = list(p0.queue) if p0 else []
            if not isinstance(arg, int) or not 0 <= arg < len(q):
                raise MusicError("That song isn't in the queue any more.")
            item = q[arg]
        p = self.check(member, action, item=item)
        async with self.lock:
            if action == "pause_toggle":
                await p.pause(not p.paused)
                msg = "⏸️ Paused." if p.paused else "▶️ Playing."
            elif action == "pause":
                await p.pause(True)
                msg = "⏸️ Paused."
            elif action == "resume":
                await p.pause(False)
                msg = "▶️ Playing."
            elif action == "skip":
                if not p.current:
                    raise MusicError("Nothing to skip.")
                await p.skip(force=True)
                msg = "⏭️ Skipped."
            elif action == "previous":
                hist = list(p.queue.history)
                if len(hist) < 2:
                    raise MusicError("There's no previous song yet.")
                prev = hist[-2]
                if p.current:
                    p.queue.put_at(0, p.current)
                await p.play(prev)
                msg = f"⏮️ Back to **{_esc(prev.title)}**."
            elif action == "shuffle":
                p.queue.shuffle()
                msg = "🔀 Shuffled."
            elif action == "loop":
                order = [wavelink.QueueMode.normal, wavelink.QueueMode.loop, wavelink.QueueMode.loop_all]
                p.queue.mode = order[(order.index(p.queue.mode) + 1) % 3] if arg is None else arg
                msg = {wavelink.QueueMode.normal: "🔁 Repeat off.", wavelink.QueueMode.loop: "🔂 Repeating this song.",
                       wavelink.QueueMode.loop_all: "🔁 Repeating the queue."}[p.queue.mode]
            elif action == "remove":
                p.queue.delete(arg)
                msg = f"🗑️ Removed **{_esc(item.title)}**."
            elif action == "clear":
                p.queue.clear()
                msg = "🧹 Queue cleared."
            elif action == "volume":
                await p.set_volume(int(arg))
                msg = f"🔊 Volume {int(arg)}%."
            elif action == "stop":
                await self._clear_247(p)
                p.queue.clear()
                await p.skip(force=True)
                msg = "⏹️ Stopped."
            elif action == "disconnect":
                await self._clear_247(p)
                await p.disconnect()
                msg = "👋 Music left the channel."
            else:
                raise MusicError("Unknown action.")
        await self.refresh_panel(p)
        return msg

    async def save_current(self, member: discord.Member, *, server: bool = False) -> str:
        p = self.player()
        t = getattr(p, "current", None) if p else None
        if not t or not t.uri or getattr(p, "station", None):
            raise MusicError("Play a song first, then save it.")
        if server and self.bot.level_of(member) < Level.OWNER:
            raise MusicError("Only the server owner can pin server favourites.")
        res = await musicdb.add_favorite(self.bot.db, member.guild.id, 0 if server else member.id,
                                         key=musicdb.track_key(t.uri, t.source, t.identifier), title=t.title, author=t.author,
                                         uri=t.uri)
        return {"added": f"❤️ Saved **{_esc(t.title)}** to {'server favourites' if server else 'your favourites'}.",
                "exists": "Already saved.", "full": "Favourites are full — remove one first."}[res]

    async def play_something(self, member, home, mood: str) -> str:
        """Mood → a reliable live station; 'surprise' → your/server favourites and real history when there is enough."""
        if mood in MOODS:
            return await self.start_station(member, home, MOODS[mood], continuous=False)
        g = member.guild
        pool = {r["uri"]: r for r in await musicdb.favorites(self.bot.db, g.id, member.id)}
        pool.update({r["uri"]: r for r in await musicdb.favorites(self.bot.db, g.id, 0)})
        pool.update({r["uri"]: r for r in await musicdb.frequent(self.bot.db, g.id, 20)})
        pool.update({r["uri"]: r for r in await musicdb.recent(self.bot.db, g.id, 20)})
        if len(pool) < 3:
            return await self.start_station(member, home, random.choice(list(STATIONS)), continuous=False)
        picks = random.sample(list(pool), min(5, len(pool)))
        first = await self.enqueue(member, home, picks[0])
        for uri in picks[1:]:
            self.last_action.pop(member.id, None)
            try:
                await self.enqueue(member, home, uri)
            except MusicError:
                pass
        return f"🎲 {first} (+{len(picks) - 1} more from your server's favourites)"

    # ---------------------------------------------------------- 24/7 radio
    async def start_station(self, member, home, key: str, *, continuous: bool = True) -> str:
        if key not in STATIONS:
            raise MusicError("Unknown station.")
        self._rate(member)
        p = await self.ensure_player(member, home)
        p0 = self.player()
        if p0 and p0.current and not getattr(p0, "station", None) and len([m for m in p0.channel.members if not m.bot]) > 1:
            self.check(member, "stop")  # replacing someone's music needs stop rights
        emoji, name, url, desc = STATIONS[key]
        found = await self.search(url, 1)
        if not found:
            raise MusicError("That station is unreachable right now — try another one.")
        async with self.lock:
            p.queue.clear()
            p.station = key
            p.starter_id = member.id
            p.autoplay = wavelink.AutoPlayMode.disabled
            if continuous:
                p.inactive_timeout = None
                await self.bot.db.kv_set("music:247", {"station": key, "channel_id": p.channel.id,
                                                      "home": getattr(p.home, "id", None), "by": member.id, "sleeping": False})
            t = found[0]
            t.extras = {"requester": member.id}
            await p.play(t)
        await self.refresh_panel(p)
        return f"📻 {'24/7 ' if continuous else ''}**{emoji} {name}** is on ({desc})."

    async def stop_station(self, member) -> str:
        p = self.player()
        if p is None or not getattr(p, "station", None):
            await self.bot.db.kv_set("music:247", None)
            return "24/7 is off."
        return await self.control(member, "stop")

    async def _clear_247(self, p):
        if getattr(p, "station", None):
            p.station = None
            p.autoplay = wavelink.AutoPlayMode.partial
            p.inactive_timeout = self.bot.cfg.music.idle_disconnect_seconds
        await self.bot.db.kv_set("music:247", None)

    async def _resume_247(self, why: str):
        st = await self.bot.db.kv_get("music:247")
        g = self.bot.guild
        if not st or not g or not self.online():
            return
        ch = g.get_channel(st["channel_id"])
        if not isinstance(ch, discord.VoiceChannel) or not any(not m.bot for m in ch.members):
            return
        p = g.voice_client or await ch.connect(cls=wavelink.Player, self_deaf=True)
        if p.channel != ch:
            await p.move_to(ch)
        p.home = g.get_channel(st.get("home") or 0) or ch
        emoji, name, url, _ = STATIONS[st["station"]]
        found = await self.search(url, 1)
        if not found:
            return
        p.station, p.starter_id = st["station"], st.get("by")
        p.autoplay, p.inactive_timeout = wavelink.AutoPlayMode.disabled, None
        await p.set_volume(self.bot.cfg.music.default_volume)
        await p.play(found[0])
        st["sleeping"] = False
        await self.bot.db.kv_set("music:247", st)
        log.info("24/7 resumed (%s): %s", why, name)

    @tasks.loop(seconds=60)
    async def watch_247(self):
        """Sleep when nobody listens (resource-friendly), resume when people return."""
        try:
            st = await self.bot.db.kv_get("music:247")
            p = self.player()
            if not st:
                return
            ch = self.bot.guild.get_channel(st["channel_id"]) if self.bot.guild else None
            humans = [m for m in getattr(ch, "members", []) if not m.bot]
            if p and getattr(p, "station", None) and not humans:
                self.empty_since = self.empty_since or time.time()
                if time.time() - self.empty_since >= self.bot.cfg.music.idle_247_minutes * 60:
                    st["sleeping"] = True
                    await self.bot.db.kv_set("music:247", st)
                    p.station = None
                    await p.disconnect()
                    self.empty_since = None
                    log.info("24/7 sleeping: nobody listening")
            else:
                self.empty_since = None
            if (p is None or not p.playing) and humans and not st.get("sleeping"):
                await self._resume_247("watchdog")
        except Exception:  # noqa: BLE001
            log.exception("24/7 watchdog")

    @watch_247.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if member.bot or not after.channel or (before.channel and before.channel.id == after.channel.id):
            return
        st = await self.bot.db.kv_get("music:247")
        if st and st.get("sleeping") and after.channel.id == st["channel_id"]:
            st["sleeping"] = False
            await self.bot.db.kv_set("music:247", st)
            await self._resume_247("someone joined")

    # ---------------------------------------------------------- live player message (one per channel, edited in place)
    def player_embed(self, p) -> discord.Embed:
        t = getattr(p, "current", None) if p else None
        if not t:
            return discord.Embed(title="🎵 Music", description="Nothing playing.", color=COLORS["MUSIC"])
        st = getattr(p, "station", None)
        if st:
            emoji, name, _, desc = STATIONS[st]
            emb = discord.Embed(title=f"📻 {emoji} {name}", description=f"Live radio · {desc}", color=COLORS["MUSIC"])
            st247 = getattr(p, "inactive_timeout", 1) is None
            emb.set_footer(text=("24/7 — sleeps when everyone leaves, wakes when someone joins" if st247 else "Radio")
                           + f" · {p.channel.name}")
            return emb
        req = requester_of(t)
        emb = discord.Embed(title="🎵 Now playing", color=COLORS["MUSIC"],
                            description=f"**{_esc(t.title)}**\n{_esc(t.author)}\n`{_fmt_ms(p.position)} / {_fmt_ms(t.length)}`"
                                        + (f" · added by <@{req}>" if req else ""))
        nxt = list(p.queue)[:3]
        if nxt:
            emb.add_field(name="Up next", value="\n".join(f"{i}. {_esc(x.title)[:60]}" for i, x in enumerate(nxt, 1)), inline=False)
        mode = {wavelink.QueueMode.normal: "", wavelink.QueueMode.loop: " · 🔂 repeat song",
                wavelink.QueueMode.loop_all: " · 🔁 repeat queue"}.get(p.queue.mode, "")
        emb.set_footer(text=f"{len(p.queue)} in queue{mode}" + (" · paused" if p.paused else "") + f" · {p.channel.name}")
        if getattr(t, "artwork", None):
            emb.set_thumbnail(url=t.artwork)
        return emb

    async def refresh_panel(self, p, *, new: bool = False):
        home = getattr(p, "home", None) if p else None
        if home is None:
            return
        view = PlayerView(paused=bool(getattr(p, "paused", False)))
        mid = self.panels.get(home.id)
        try:
            if mid and not new:
                await home.get_partial_message(mid).edit(embed=self.player_embed(p), view=view)
                return
        except discord.HTTPException:
            pass
        try:
            if mid:  # keep exactly one player message: the old one goes when a fresh one is posted
                try:
                    await home.get_partial_message(mid).delete()
                except discord.HTTPException:
                    pass
            msg = await home.send(embed=self.player_embed(p), view=view, allowed_mentions=discord.AllowedMentions.none())
            self.panels[home.id] = msg.id
        except discord.HTTPException:
            log.info("cannot post player in %s", getattr(home, "id", "?"))

    async def _close_panel(self, p):
        home = getattr(p, "home", None)
        mid = self.panels.pop(getattr(home, "id", 0), None)
        if home and mid:
            try:
                await home.get_partial_message(mid).edit(embed=discord.Embed(description="🎵 Music stopped.", color=COLORS["MUSIC"]),
                                                         view=None)
            except discord.HTTPException:
                pass

    async def button(self, interaction: discord.Interaction, action: str):
        """Player-message buttons."""
        try:
            if action == "save":
                msg = await self.save_current(interaction.user)
            else:
                msg = await self.control(interaction.user, action)
            await interaction.response.send_message(msg, ephemeral=True, delete_after=6,
                                                    allowed_mentions=discord.AllowedMentions.none())
        except MusicError as e:
            await interaction.response.send_message(f"⚠️ {e}", ephemeral=True, delete_after=8)

    # ---------------------------------------------------------- wavelink events
    @commands.Cog.listener()
    async def on_wavelink_node_ready(self, payload):
        self.state = "online"
        log.info("Lavalink node ready: %s (resumed=%s)", payload.node.identifier, payload.resumed)
        await asyncio.sleep(3)
        try:
            await self._resume_247("startup")
        except Exception:  # noqa: BLE001
            log.exception("24/7 resume at startup")

    @commands.Cog.listener()
    async def on_wavelink_inactive_player(self, player):
        await self._close_panel(player)
        await player.disconnect()

    @commands.Cog.listener()
    async def on_wavelink_track_start(self, payload):
        p, t = payload.player, payload.track
        await self.refresh_panel(p, new=not self.panels.get(getattr(getattr(p, "home", None), "id", 0)))
        if getattr(p, "station", None) or t.is_stream or not requester_of(t):
            return  # only member-requested songs count (no radio, no self-test tracks)
        try:
            await musicdb.record_play(self.bot.db, p.guild.id, key=musicdb.track_key(t.uri, t.source, t.identifier),
                                      title=t.title, author=t.author, uri=t.uri, source=t.source, length_ms=t.length,
                                      requester_id=requester_of(t), channel_id=p.channel.id if p.channel else None)
        except Exception:  # noqa: BLE001
            log.exception("record play")

    @commands.Cog.listener()
    async def on_wavelink_track_end(self, payload):
        p = payload.player
        if p and getattr(p, "station", None) and payload.reason in ("loadFailed", "finished"):
            await self._stream_retry(p)
        elif p and not p.playing and not p.queue:
            await self._close_panel(p)

    @commands.Cog.listener()
    async def on_wavelink_track_exception(self, payload):
        if payload.player and getattr(payload.player, "station", None):
            await self._stream_retry(payload.player)

    async def _stream_retry(self, p):
        """Radio streams can drop; retry with backoff, give up (and say so once) after 5 failures in 10 min."""
        now = time.time()
        self.stream_failures = [t for t in self.stream_failures if now - t < 600] + [now]
        if len(self.stream_failures) > 5:
            log.warning("24/7 stream keeps failing; stopping")
            await self._clear_247(p)
            home = getattr(p, "home", None)
            if home:
                try:
                    await home.send("📻 The radio station keeps dropping, so I stopped it. Try another station.", delete_after=120)
                except discord.HTTPException:
                    pass
            return
        await asyncio.sleep(2 ** len(self.stream_failures))
        key = getattr(p, "station", None)
        if key:
            found = await self.search(STATIONS[key][2], 1)
            if found:
                await p.play(found[0])

    # ---------------------------------------------------------- slash commands (advanced fallback; same service)
    async def _reply(self, interaction: discord.Interaction, coro, ephemeral: bool = False):
        try:
            msg = await coro
        except MusicError as e:
            msg, ephemeral = f"⚠️ {e}", True
        send = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
        await send(embed=discord.Embed(description=msg, color=COLORS["MUSIC"]), ephemeral=ephemeral,
                   allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="play", description="Play a song/playlist link or search term")
    async def play(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer(thinking=True)
        await self._reply(interaction, self.enqueue(interaction.user, interaction.channel, query))

    @app_commands.command(name="pause", description="Pause")
    async def pause(self, interaction: discord.Interaction):
        await self._reply(interaction, self.control(interaction.user, "pause"))

    @app_commands.command(name="resume", description="Resume")
    async def resume(self, interaction: discord.Interaction):
        await self._reply(interaction, self.control(interaction.user, "resume"))

    @app_commands.command(name="skip", description="Skip the current song")
    async def skip(self, interaction: discord.Interaction):
        await self._reply(interaction, self.control(interaction.user, "skip"))

    @app_commands.command(name="previous", description="Play the previous song again")
    async def previous(self, interaction: discord.Interaction):
        await self._reply(interaction, self.control(interaction.user, "previous"))

    @app_commands.command(name="queue", description="Show the queue")
    async def queue(self, interaction: discord.Interaction):
        await self.bot.get_cog("ControlCenter").open(interaction, "music_queue")

    @app_commands.command(name="nowplaying", description="What's playing")
    async def nowplaying(self, interaction: discord.Interaction):
        await interaction.response.send_message(embed=self.player_embed(self.player()), ephemeral=True)

    @app_commands.command(name="volume", description="Set volume 0-150")
    async def volume(self, interaction: discord.Interaction, percent: app_commands.Range[int, 0, 150]):
        await self._reply(interaction, self.control(interaction.user, "volume", percent))

    @app_commands.command(name="shuffle", description="Shuffle the queue")
    async def shuffle(self, interaction: discord.Interaction):
        await self._reply(interaction, self.control(interaction.user, "shuffle"))

    @app_commands.command(name="loop", description="Repeat mode")
    @app_commands.choices(mode=[app_commands.Choice(name=n, value=n) for n in ("off", "track", "queue")])
    async def loop(self, interaction: discord.Interaction, mode: str):
        m = {"off": wavelink.QueueMode.normal, "track": wavelink.QueueMode.loop, "queue": wavelink.QueueMode.loop_all}[mode]
        await self._reply(interaction, self.control(interaction.user, "loop", m))

    @app_commands.command(name="stop", description="Stop and clear the queue")
    async def stop(self, interaction: discord.Interaction):
        await self._reply(interaction, self.control(interaction.user, "stop"))

    @app_commands.command(name="disconnect", description="Make the music leave the channel")
    async def disconnect(self, interaction: discord.Interaction):
        await self._reply(interaction, self.control(interaction.user, "disconnect"))


async def setup(bot):
    await bot.add_cog(Music(bot))
