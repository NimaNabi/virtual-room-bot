"""/voice — "Join to Create" temporary voice rooms with a button control panel.

Join ➕ Create Room → the bot creates "<name>'s Room" in the same category (inheriting its permissions),
moves you in, makes you the owner, posts a control panel in the room's chat, and deletes the room when it is
empty. Controls: buttons (lock/unlock, rename, limit) and user pickers (permit, reject, disconnect, transfer);
the same actions exist as /voice commands.
Abuse limits: 1 room per person, 3 creations per 10 min per person, global max rooms, 15 s cooldown.
"""
from __future__ import annotations

import asyncio
import re
import logging
import time

import discord
from discord import app_commands
from discord.ext import commands

from ..authz import Level
from ..bot import require_level
from ..events import bot_reason
from ..ui import COLORS, ConfirmView

log = logging.getLogger("vrbot.voice")


class RoomError(app_commands.CheckFailure):
    pass


# ---------------------------------------------------------------- panel UI (persistent: custom_ids are fixed)
NAME_BAD = re.compile(r"(discord\.gg/|discord(app)?\.com/invite|https?://|@everyone|@here|<@|<#|<:)", re.I)


def clean_name(text: str) -> str:
    """Room names: no invite links, URLs or mentions; trimmed to 60 chars."""
    t = NAME_BAD.sub("", text or "").strip()
    return (t or "Room")[:60]


class RenameModal(discord.ui.Modal, title="Rename your room"):
    name = discord.ui.TextInput(label="New name", max_length=60)

    def __init__(self, cog, ch):
        super().__init__()
        self.cog, self.ch = cog, ch

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.op(interaction, self.ch, "rename", str(self.name))


class LimitModal(discord.ui.Modal, title="User limit"):
    limit = discord.ui.TextInput(label="Max people (0 = no limit)", max_length=2)

    def __init__(self, cog, ch):
        super().__init__()
        self.cog, self.ch = cog, ch

    async def on_submit(self, interaction: discord.Interaction):
        v = str(self.limit).strip()
        if not v.isdigit() or int(v) > 99:
            await interaction.response.send_message("Enter a number 0–99.", ephemeral=True)
            return
        await self.cog.op(interaction, self.ch, "limit", int(v))


class MemberPick(discord.ui.UserSelect):
    def __init__(self):
        super().__init__(placeholder="👤 Choose a member, then an action below", min_values=1, max_values=1,
                         custom_id="vr:pick", row=0)

    async def callback(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("VoiceRooms")
        target = self.values[0]
        cog.picks[(interaction.channel.id, interaction.user.id)] = target.id
        await interaction.response.send_message(f"Selected **{target.display_name}** — now choose an action.",
                                                ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


def _member_action(action: str, label: str, emoji: str, row: int, style=discord.ButtonStyle.secondary):
    @discord.ui.button(label=label, emoji=emoji, style=style, custom_id=f"vr:{action}", row=row)
    async def handler(self, interaction: discord.Interaction, button):
        cog = interaction.client.get_cog("VoiceRooms")
        mid = cog.picks.get((interaction.channel.id, interaction.user.id))
        target = interaction.guild.get_member(mid) if mid else None
        if target is None:
            await interaction.response.send_message("Choose a member in the menu first.", ephemeral=True)
            return
        await cog.op(interaction, interaction.channel, action, target)
    return handler


class RoomPanel(discord.ui.View):
    """One persistent control panel per room (fixed custom_ids survive restarts)."""

    def __init__(self, room: discord.VoiceChannel | None = None, guest_ok: bool = True):
        super().__init__(timeout=None)
        self.add_item(MemberPick())
        if not guest_ok:  # the room OWNER can't sponsor guests: don't show a button that can only refuse
            self.remove_item(self.b_guest)
        if room is not None:  # show the action that makes sense now (Lock ↔ Unlock, Hide ↔ Show)
            ev = room.overwrites_for(room.guild.default_role)
            self.b_lock.label, self.b_lock.emoji = ("Unlock", "🔓") if ev.connect is False else ("Lock", "🔒")
            self.b_hide.label, self.b_hide.emoji = ("Show", "👁️") if ev.view_channel is False else ("Hide", "🙈")

    b_trust = _member_action("trust", "Trust", "✅", 1, discord.ButtonStyle.success)
    b_untrust = _member_action("untrust", "Untrust", "➖", 1)
    b_block = _member_action("block", "Block", "🚫", 1, discord.ButtonStyle.danger)
    b_unblock = _member_action("unblock", "Unblock", "♻️", 1)
    b_invite = _member_action("invite", "Invite", "📨", 1, discord.ButtonStyle.primary)
    b_disconnect = _member_action("disconnect", "Disconnect", "👋", 2)
    b_transfer = _member_action("transfer", "Make owner", "👑", 2)

    @discord.ui.button(label="Claim", emoji="🙋", style=discord.ButtonStyle.secondary, custom_id="vr:claim", row=2)
    async def b_claim(self, interaction, button):
        await interaction.client.get_cog("VoiceRooms").claim_room(interaction, interaction.channel)

    @discord.ui.button(label="Close", emoji="🗑️", style=discord.ButtonStyle.danger, custom_id="vr:close", row=2)
    async def b_close(self, interaction, button):
        await interaction.client.get_cog("VoiceRooms").ask_close(interaction, interaction.channel)

    @discord.ui.button(label="Invite a friend", emoji="🎟️", style=discord.ButtonStyle.primary, custom_id="vr:guest", row=2)
    async def b_guest(self, interaction, button):
        cog = interaction.client.get_cog("VoiceRooms")
        if not cog.owner_check(interaction, interaction.channel, send=False):
            await interaction.response.send_message("Only the room owner can do that.", ephemeral=True)
            return
        guests = interaction.client.get_cog("Guests")
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, msg = await guests.create_invite(interaction.user, room=interaction.channel)
        await interaction.followup.send(embed=discord.Embed(description=msg, color=COLORS["OK"] if ok else COLORS["WARNING"]),
                                        ephemeral=True)

    @discord.ui.button(label="Lock", emoji="🔒", style=discord.ButtonStyle.secondary, custom_id="vr:locktoggle", row=3)
    async def b_lock(self, interaction, button):
        ch = interaction.channel
        locked = ch.overwrites_for(ch.guild.default_role).connect is False
        await interaction.client.get_cog("VoiceRooms").op(interaction, ch, "unlock" if locked else "lock")

    @discord.ui.button(label="Hide", emoji="🙈", style=discord.ButtonStyle.secondary, custom_id="vr:hidetoggle", row=3)
    async def b_hide(self, interaction, button):
        ch = interaction.channel
        hidden = ch.overwrites_for(ch.guild.default_role).view_channel is False
        await interaction.client.get_cog("VoiceRooms").op(interaction, ch, "show" if hidden else "hide")

    @discord.ui.button(label="Rename", emoji="✏️", style=discord.ButtonStyle.secondary, custom_id="vr:rename", row=3)
    async def b_rename(self, interaction, button):
        cog = interaction.client.get_cog("VoiceRooms")
        if cog.owner_check(interaction, interaction.channel):
            await interaction.response.send_modal(RenameModal(cog, interaction.channel))
        else:
            await interaction.response.send_message("Only the room owner can do that.", ephemeral=True)

    @discord.ui.button(label="Limit", emoji="👥", style=discord.ButtonStyle.secondary, custom_id="vr:limit", row=3)
    async def b_limit(self, interaction, button):
        cog = interaction.client.get_cog("VoiceRooms")
        if cog.owner_check(interaction, interaction.channel):
            await interaction.response.send_modal(LimitModal(cog, interaction.channel))
        else:
            await interaction.response.send_message("Only the room owner can do that.", ephemeral=True)


class VoiceRooms(commands.GroupCog, group_name="voice", group_description="Your temporary voice room"):
    def __init__(self, bot):
        self.bot = bot
        self.rooms: dict[int, int] = {}        # channel_id -> owner_id
        self.cooldown: dict[int, float] = {}
        self.history: dict[int, list[float]] = {}
        self.lock = asyncio.Lock()
        self.test_member_id: int | None = None  # set only by the self-test (lets the bot use the hub itself)
        self.meta: dict[int, dict] = {}         # room -> {"panel": msg id, "trusted": [...], "blocked": [...]}
        self.picks: dict[tuple, int] = {}       # (room, clicking user) -> selected member
        super().__init__()

    async def cog_load(self):
        self.rooms = {int(k): v for k, v in (await self.bot.db.kv_get("voicerooms:rooms", {})).items()}
        self.meta = {int(k): v for k, v in (await self.bot.db.kv_get("voicerooms:meta", {})).items()}
        self.bot.add_view(RoomPanel())
        self.bot.tree.add_command(app_commands.ContextMenu(name="Pull into my voice", callback=self.pull))

    async def _save(self):
        await self.bot.db.kv_set("voicerooms:rooms", {str(k): v for k, v in self.rooms.items()})
        self.meta = {k: v for k, v in self.meta.items() if k in self.rooms}
        await self.bot.db.kv_set("voicerooms:meta", {str(k): v for k, v in self.meta.items()})

    async def hub(self) -> discord.VoiceChannel | None:
        g = self.bot.guild
        if not g:
            return None
        cid = await self.bot.db.kv_get("voicerooms:create_channel_id")
        ref = str(cid) if cid else (self.bot.cfg.voice_rooms.create_channel if self.bot.cfg.voice_rooms.enabled else None)
        if not ref:
            return None
        ch = g.get_channel(int(ref)) if ref.isdigit() else discord.utils.find(lambda c: c.name.lower() == ref.lower(), g.voice_channels)
        return ch if isinstance(ch, discord.VoiceChannel) else None

    # ---------------------------------------------------------- lifecycle
    @commands.Cog.listener()
    async def on_ready(self):
        g = self.bot.guild
        if not g:
            return
        for cid in list(self.rooms):  # no orphans: rooms emptied while offline are removed
            ch = g.get_channel(cid)
            if ch is None:
                self.rooms.pop(cid, None)
            elif not ch.members and not self.bot.safe_mode():
                await self._delete(ch, "empty after restart")
        await self._save()

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        g = self.bot.guild
        if not g or member.guild.id != g.id or (member.bot and member.id != self.test_member_id):
            return
        hub = await self.hub()
        if hub and after.channel and after.channel.id == hub.id:
            await self._create_for(member, hub)
        left = before.channel if before.channel and (not after.channel or after.channel.id != before.channel.id) else None
        if left and left.id in self.rooms:
            if not left.members:
                await self._delete(left, "empty")
            elif self.rooms[left.id] == member.id:  # owner left: hand the room to someone still inside
                heir = next((m for m in left.members if not m.bot), None)
                if heir:
                    self.rooms[left.id] = heir.id
                    await self._save()
                    await self.refresh_panel(left)
                    await self.bot.db.add_event(type="voice_room_transfer", category="voice", guild_id=g.id, target_id=heir.id,
                                                target_name=heir.display_name, actor_name="the bot (owner left)",
                                                actor_confidence="confirmed", channel_id=left.id, channel_name=left.name, source="bot")

    def _rate_ok(self, uid: int) -> bool:
        now = time.time()
        if now - self.cooldown.get(uid, 0) < 15:
            return False
        recent = [t for t in self.history.get(uid, []) if now - t < 600]
        if len(recent) >= 3:
            return False
        self.cooldown[uid] = now
        self.history[uid] = recent + [now]
        return True

    async def _create_for(self, member: discord.Member, hub: discord.VoiceChannel) -> discord.VoiceChannel | None:
        if self.bot.safe_mode():
            return None
        async with self.lock:
            owned = [cid for cid, o in self.rooms.items() if o == member.id and member.guild.get_channel(cid)]
            if owned:  # one room per person: send them back to it
                room = member.guild.get_channel(owned[0])
                await member.move_to(room)
                return room
            if not self._rate_ok(member.id) or len(self.rooms) >= self.bot.cfg.voice_rooms.max_rooms:
                try:
                    await member.move_to(None)
                except discord.HTTPException:
                    pass
                return None
            cfg = self.bot.cfg.voice_rooms
            cat = hub.category
            overwrites = dict(cat.overwrites) if cat else dict(hub.overwrites)
            room = await member.guild.create_voice_channel(
                cfg.name_template.format(name=member.display_name)[:100], category=cat, overwrites=overwrites,
                user_limit=cfg.default_limit, bitrate=min(96000, int(member.guild.bitrate_limit)),
                reason=bot_reason("the bot", self.bot.user.id, f"temporary room for {member}"))
            self.rooms[room.id] = member.id
            await self._save()
        try:
            await member.move_to(room)
        except discord.HTTPException:
            pass
        await self.post_panel(room, member)
        await self.bot.db.add_event(type="voice_room_create", category="voice", guild_id=member.guild.id,
                                    target_id=member.id, target_name=member.display_name, actor_confidence="self",
                                    channel_id=room.id, channel_name=room.name, source="bot")
        return room

    def guest_ok(self, room) -> bool:
        owner = room.guild.get_member(self.rooms.get(room.id) or 0)
        guests = self.bot.get_cog("Guests")
        return bool(owner and guests and guests.sponsor_ok(owner))

    async def ask_close(self, interaction: discord.Interaction, ch):
        """Owner closes the room (everyone inside is disconnected). Confirmation first."""
        if not ch or ch.id not in self.rooms:
            await interaction.response.send_message("That room is already gone.", ephemeral=True)
            return
        if not self.owner_check(interaction, ch, send=False):
            await interaction.response.send_message("This room belongs to someone else.", ephemeral=True)
            return

        async def go(i: discord.Interaction):
            self.bot.guard("voice_room", i.user.id)
            await self.bot.db.add_event(type="voice_room_close", category="voice", guild_id=ch.guild.id, target_id=i.user.id,
                                        target_name=i.user.display_name, actor_id=i.user.id, actor_name=i.user.display_name,
                                        actor_confidence="confirmed", channel_id=ch.id, channel_name=ch.name, source="bot")
            await self._delete(ch, f"closed by {i.user.display_name}")
        await interaction.response.send_message(f"Close **{discord.utils.escape_markdown(ch.name)}**? Everyone inside is disconnected.",
                                                view=ConfirmView(interaction.user.id, go, confirm_label="Close room"), ephemeral=True)

    def panel_embed(self, room: discord.VoiceChannel) -> discord.Embed:
        meta = self.meta.get(room.id, {})
        owner_id = self.rooms.get(room.id)
        ev = room.overwrites_for(room.guild.default_role)
        state = ("🔒 locked" if ev.connect is False else "🔓 open") + " · " + ("👁️ hidden" if ev.view_channel is False else "visible")
        emb = discord.Embed(title=f"🔊 {room.name}", color=COLORS["VOICE"],
                            description=f"**Owner:** <@{owner_id}>\n**State:** {state} · limit {room.user_limit or '∞'}\n"
                                        f"**Trusted:** {len(meta.get('trusted', []))} · **Blocked:** {len(meta.get('blocked', []))}\n\n"
                                        "Pick someone in the menu, then an action. Room settings are in the bottom row. "
                                        "The room disappears when everyone leaves.")
        emb.set_footer(text="Only the room owner can use these controls")
        return emb

    async def post_panel(self, room: discord.VoiceChannel, owner: discord.Member):
        try:
            msg = await room.send(embed=self.panel_embed(room), view=RoomPanel(room, self.guest_ok(room)), allowed_mentions=discord.AllowedMentions.none())
            self.meta.setdefault(room.id, {"trusted": [], "blocked": []})["panel"] = msg.id
            await self._save()
        except discord.HTTPException:
            log.info("could not post panel in %s", room.id)

    async def refresh_panel(self, room: discord.VoiceChannel):
        """Edit the single panel message in place (no new panel per change)."""
        mid = self.meta.get(room.id, {}).get("panel")
        if not mid:
            return
        try:
            msg = room.get_partial_message(mid)
            await msg.edit(embed=self.panel_embed(room), view=RoomPanel(room, self.guest_ok(room)))
        except discord.HTTPException:
            pass

    async def claim_room(self, interaction: discord.Interaction, ch):
        if not ch or ch.id not in self.rooms or not interaction.user.voice or interaction.user.voice.channel != ch:
            await interaction.response.send_message("Join the room first.", ephemeral=True)
            return
        owner = ch.guild.get_member(self.rooms[ch.id])
        if owner and owner.voice and owner.voice.channel == ch:
            await interaction.response.send_message(f"{owner.display_name} is still here.", ephemeral=True)
            return
        self.rooms[ch.id] = interaction.user.id
        await self._save()
        await self.refresh_panel(ch)
        await interaction.response.send_message("👑 You own this room now.", ephemeral=True)

    async def _delete(self, ch: discord.VoiceChannel, why: str):
        if self.bot.safe_mode():
            return
        try:
            await ch.delete(reason=bot_reason("the bot", self.bot.user.id, f"temporary room {why}"))
        except discord.NotFound:
            pass
        except discord.HTTPException:
            log.warning("could not delete room %s", ch.id, exc_info=True)
            return
        self.rooms.pop(ch.id, None)
        await self._save()
        await self.bot.db.add_event(type="voice_room_delete", category="voice", guild_id=ch.guild.id, channel_id=ch.id,
                                    channel_name=ch.name, actor_confidence="self", details={"why": why}, source="bot")

    # ---------------------------------------------------------- shared control logic (buttons + commands)
    def owner_check(self, interaction: discord.Interaction, ch, send: bool = True) -> bool:
        if not ch or getattr(ch, "id", None) not in self.rooms:
            return False
        return self.rooms[ch.id] == interaction.user.id or self.bot.level_of(interaction.user) >= Level.MOD

    async def op(self, interaction: discord.Interaction, ch, action: str, arg=None):
        try:
            if not ch or ch.id not in self.rooms:
                raise RoomError("This only works inside a temporary room.")
            if not self.owner_check(interaction, ch):
                raise RoomError("Only the room owner can do that (`/voice claim` if the owner left).")
            msg = await self.do(ch, action, arg, actor=interaction.user)
        except (RoomError, app_commands.CheckFailure) as e:
            msg = f"⚠️ {e}"
        send = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
        await send(msg, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    async def do(self, ch: discord.VoiceChannel, action: str, arg=None, actor=None) -> str:
        """Perform a room action (no authorization here — callers check). Returns a user message."""
        self.bot.guard("voice_room", getattr(actor, "id", None))
        reason = bot_reason(getattr(actor, "display_name", "the bot"), getattr(actor, "id", self.bot.user.id), f"room {action}")
        everyone = ch.guild.default_role
        msg = await self._do(ch, action, arg, actor, reason, everyone)
        # every room action is owner evidence: who did what to whom, in which room
        member = arg if isinstance(arg, discord.Member) else None
        if action != "transfer":  # transfer logs its own event
            await self.bot.db.add_event(
                type=f"voice_room_{ {'permit': 'trust', 'reject': 'block'}.get(action, action)}" if member else "voice_room_settings",
                category="voice", guild_id=ch.guild.id,
                target_id=member.id if member else None, target_name=member.display_name if member else None,
                actor_id=getattr(actor, "id", None), actor_name=getattr(actor, "display_name", None),
                actor_confidence="confirmed", channel_id=ch.id, channel_name=ch.name,
                details={"action": action, **({} if member else {"value": str(arg)[:60] if arg is not None else None})},
                source="bot")
        await self.refresh_panel(ch)
        return msg

    async def _do(self, ch, action, arg, actor, reason, everyone) -> str:
        meta = self.meta.setdefault(ch.id, {"trusted": [], "blocked": []})
        if action in ("permit", "reject"):  # legacy command names
            action = {"permit": "trust", "reject": "block"}[action]
        if action == "rename":
            name = clean_name(str(arg))
            await ch.edit(name=name, reason=reason)
            return f"✏️ Renamed to **{discord.utils.escape_markdown(name)}**."
        if action == "limit":
            await ch.edit(user_limit=int(arg), reason=reason)
            return f"👥 Limit: {arg or 'none'}."
        if action == "lock":
            ow = ch.overwrites_for(everyone)
            ow.connect = False
            await ch.set_permissions(everyone, overwrite=ow, reason=reason)
            for m in ch.members:
                await ch.set_permissions(m, connect=True, reason=reason)
            return "🔒 Locked: only people inside or permitted can join."
        if action == "unlock":
            ow = ch.overwrites_for(everyone)
            ow.connect = None
            await ch.set_permissions(everyone, overwrite=None if ow.is_empty() else ow, reason=reason)
            return "🔓 Unlocked."
        if action == "hide":
            ow = ch.overwrites_for(everyone)
            ow.view_channel = False
            await ch.set_permissions(everyone, overwrite=ow, reason=reason)
            for m in ch.members:
                await ch.set_permissions(m, view_channel=True, connect=True, reason=reason)
            return "👁️ Hidden: only people inside or permitted can see the room."
        if action == "show":
            ow = ch.overwrites_for(everyone)
            ow.view_channel = None
            await ch.set_permissions(everyone, overwrite=None if ow.is_empty() else ow, reason=reason)
            return "👁️ Visible again."
        m: discord.Member = arg
        if m is None:
            raise RoomError("Pick a member.")
        if action == "trust":
            await ch.set_permissions(m, connect=True, view_channel=True, reason=reason)
            meta["trusted"] = sorted(set(meta.get("trusted", [])) | {m.id})
            meta["blocked"] = [x for x in meta.get("blocked", []) if x != m.id]
            await self._save()
            return f"✅ {m.mention} is trusted (can see and join even when locked/hidden)."
        if action in ("untrust", "unblock"):
            if m.id == self.rooms.get(ch.id):
                raise RoomError("That's the owner.")
            await ch.set_permissions(m, overwrite=None, reason=reason)
            key = "trusted" if action == "untrust" else "blocked"
            meta[key] = [x for x in meta.get(key, []) if x != m.id]
            await self._save()
            return f"{'➖ No longer trusted' if action == 'untrust' else '♻️ Unblocked'}: {m.mention}."
        if action == "invite":
            await ch.set_permissions(m, connect=True, view_channel=True, reason=reason)
            meta["trusted"] = sorted(set(meta.get("trusted", [])) | {m.id})
            await self._save()
            inv = await ch.create_invite(max_uses=1, max_age=3600, unique=True, reason=reason)
            try:
                await m.send(f"📨 **{getattr(actor, 'display_name', 'Someone')}** invited you to **{ch.name}** in "
                             f"{ch.guild.name}: {inv.url} (one use, 1 hour)")
                return f"📨 Invited {m.mention} (DM sent, single-use link)."
            except discord.HTTPException:
                return f"📨 {m.mention} is trusted. Their DMs are closed — share this single-use link: {inv.url}"
        if action in ("block", "disconnect"):
            if actor and m.id == actor.id:
                raise RoomError("That's you.")
            if self.bot.level_of(m) >= Level.MOD and m.id != self.test_member_id:
                raise RoomError("You cannot remove a moderator.")
            if action == "block":
                await ch.set_permissions(m, connect=False, view_channel=False, reason=reason)
                meta["blocked"] = sorted(set(meta.get("blocked", [])) | {m.id})
                meta["trusted"] = [x for x in meta.get("trusted", []) if x != m.id]
                await self._save()
            if m.voice and m.voice.channel == ch:
                await m.move_to(None, reason=reason)
            elif action == "disconnect":
                raise RoomError("They are not in your room.")
            return f"{'🚫 Blocked' if action == 'block' else '👋 Disconnected'} {m.mention}."
        if action == "transfer":
            if m.bot or not m.voice or m.voice.channel != ch:
                raise RoomError("The new owner must be in the room.")
            self.rooms[ch.id] = m.id
            await self._save()
            await self.bot.db.add_event(type="voice_room_transfer", category="voice", guild_id=ch.guild.id, target_id=m.id,
                                        target_name=m.display_name, actor_id=getattr(actor, "id", None),
                                        actor_name=getattr(actor, "display_name", None), actor_confidence="confirmed",
                                        channel_id=ch.id, channel_name=ch.name, source="bot")
            return f"👑 {m.mention} now owns this room."
        raise RoomError(f"Unknown action {action}")

    # ---------------------------------------------------------- scoped moves (replace raw Move Members)
    NEUTRAL = "Can't move that member there."

    def _can_move(self, actor: discord.Member, target: discord.Member, dest) -> bool:
        from ..authz import Level
        from ..perms.privacy import area_tier, member_tier
        p = self.bot.cfg.privacy
        if target.bot or not target.voice or not target.voice.channel or not isinstance(dest, discord.VoiceChannel):
            return False
        is_owner = self.bot.level_of(actor) >= Level.OWNER
        if not is_owner:
            if not p.tiers or member_tier(p, {r.id for r in actor.roles}) == p.public_key:
                return False  # only Level members (and the owner) may move people
            if self.bot.level_of(target) >= Level.OWNER:
                return False
        src = target.voice.channel
        for ch in (src, dest):
            if not ch.permissions_for(actor).view_channel:
                return False
        if not dest.permissions_for(target).view_channel:
            return False
        key0 = min(p.tiers, key=lambda k: p.tiers[k].rank) if p.tiers else None
        for ch in (src, dest):  # Owner area is only ever reached through /owner_room (owner)
            tier = p.areas.get(str(ch.id)) or (p.areas.get(str(ch.category_id)) if ch.category_id else None)
            if tier == key0:
                return False
        return True

    async def move_service(self, actor: discord.Member, target: discord.Member, dest) -> str:
        """Scoped move (same rules for the context menu, /voice move and the Control Center)."""
        now = time.time()
        recent = [t for t in self.history.get(("mv", actor.id), []) if now - t < 60]
        if not (len(recent) < 10 and self._can_move(actor, target, dest)):
            return self.NEUTRAL  # identical answer whatever the reason: reveals nothing about hidden rooms
        self.bot.guard("voice_move", actor.id)
        src = target.voice.channel
        await target.move_to(dest, reason=bot_reason(actor.display_name, actor.id, "scoped move"))
        self.history[("mv", actor.id)] = recent + [now]
        await self.bot.db.add_event(type="voice_bot_move", category="voice", guild_id=dest.guild.id, target_id=target.id,
                                    target_name=target.display_name, actor_id=actor.id, actor_name=actor.display_name,
                                    actor_confidence="confirmed", channel_id=dest.id, channel_name=dest.name,
                                    details={"from_name": src.name, "to_name": dest.name, "via": "the bot"}, source="bot")
        return f"🔀 Moved {target.mention} to **{dest.name}**."

    async def scoped_move(self, interaction: discord.Interaction, target: discord.Member, dest) -> None:
        msg = await self.move_service(interaction.user, target, dest)
        send = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
        await send(msg, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="move", description="Move someone to a voice room you can both see")
    async def move(self, interaction: discord.Interaction, member: discord.Member, channel: discord.VoiceChannel):
        await self.scoped_move(interaction, member, channel)

    async def pull(self, interaction: discord.Interaction, member: discord.Member):
        vs = interaction.user.voice
        if not vs or not vs.channel:
            await interaction.response.send_message("Join a voice room first.", ephemeral=True)
            return
        await self.scoped_move(interaction, member, vs.channel)

    def _my_room(self, interaction: discord.Interaction):
        vs = interaction.user.voice
        return vs.channel if vs and vs.channel and vs.channel.id in self.rooms else None

    # ---------------------------------------------------------- slash commands (same logic)
    @app_commands.command(name="rename", description="Rename your room")
    async def rename(self, interaction: discord.Interaction, name: app_commands.Range[str, 1, 60]):
        await self.op(interaction, self._my_room(interaction), "rename", name)

    @app_commands.command(name="limit", description="Set a user limit (0 = none)")
    async def limit(self, interaction: discord.Interaction, users: app_commands.Range[int, 0, 99]):
        await self.op(interaction, self._my_room(interaction), "limit", users)

    @app_commands.command(name="lock", description="Only people inside (or permitted) can join")
    async def lock_(self, interaction: discord.Interaction):
        await self.op(interaction, self._my_room(interaction), "lock")

    @app_commands.command(name="unlock", description="Anyone who can see the room can join again")
    async def unlock(self, interaction: discord.Interaction):
        await self.op(interaction, self._my_room(interaction), "unlock")

    @app_commands.command(name="trust", description="Let someone see and join your room even when locked/hidden")
    async def trust(self, interaction: discord.Interaction, member: discord.Member):
        await self.op(interaction, self._my_room(interaction), "trust", member)

    @app_commands.command(name="invite", description="Send someone a single-use invite to your room")
    async def invite(self, interaction: discord.Interaction, member: discord.Member):
        await self.op(interaction, self._my_room(interaction), "invite", member)

    @app_commands.command(name="block", description="Block someone from your room (and remove them)")
    async def block(self, interaction: discord.Interaction, member: discord.Member):
        await self.op(interaction, self._my_room(interaction), "block", member)

    @app_commands.command(name="disconnect", description="Remove someone from your room (they may rejoin unless rejected)")
    async def disconnect(self, interaction: discord.Interaction, member: discord.Member):
        await self.op(interaction, self._my_room(interaction), "disconnect", member)

    @app_commands.command(name="transfer", description="Give your room to someone inside it")
    async def transfer(self, interaction: discord.Interaction, member: discord.Member):
        await self.op(interaction, self._my_room(interaction), "transfer", member)

    @app_commands.command(name="claim", description="Take over a room whose owner has left")
    async def claim(self, interaction: discord.Interaction):
        await self.claim_room(interaction, self._my_room(interaction))

    @app_commands.command(name="panel", description="Show the room control panel again")
    async def panel(self, interaction: discord.Interaction):
        ch = self._my_room(interaction)
        if not ch:
            raise app_commands.CheckFailure("Join a temporary room first.")
        await interaction.response.send_message("Room controls:", view=RoomPanel(), ephemeral=True)

    @app_commands.command(name="setup", description="Create the ➕ Create Room channel (owner)")
    @require_level("owner")
    async def setup_(self, interaction: discord.Interaction, category: discord.CategoryChannel, name: str = "➕ Create Room"):
        async def do(i: discord.Interaction):
            self.bot.guard("channel_create", i.user.id)
            hub = await category.create_voice_channel(name, reason=bot_reason(i.user.display_name, i.user.id, "voice room hub"))
            await self.bot.db.kv_set("voicerooms:create_channel_id", hub.id)
            await i.followup.send(f"✅ {hub.mention} created.", ephemeral=True)

        await interaction.response.send_message(f"Create **{name}** in **{category.name}**?",
                                                view=ConfirmView(interaction.user.id, do, "Create", danger=False), ephemeral=True)

    @app_commands.command(name="disable", description="Turn temporary rooms off (owner)")
    @require_level("owner")
    async def disable(self, interaction: discord.Interaction):
        await self.bot.db.kv_set("voicerooms:create_channel_id", None)
        await interaction.response.send_message("Temporary rooms disabled.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(VoiceRooms(bot))
