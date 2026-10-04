"""Server event logging with honest audit-log attribution."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import discord
from discord.ext import commands

from ..events import (AuditRec, category_of, diff_member, diff_voice, parse_bot_reason, utcnow)
from ..perms import flags as F

log = logging.getLogger("vrbot.eventlog")
CORRELATE_DELAY = 2.0

# audit actions logged directly as structural events (they carry executor + target + changes)
STRUCTURAL = {
    "channel_create", "channel_update", "channel_delete", "overwrite_create", "overwrite_update",
    "overwrite_delete", "role_create", "role_update", "role_delete", "guild_update", "bot_add",
    "member_prune", "webhook_create", "webhook_update", "webhook_delete", "emoji_create", "emoji_delete",
    "integration_create", "integration_delete", "automod_rule_create", "automod_rule_update",
    "automod_rule_delete", "thread_delete", "invite_create", "invite_delete",
}
EVENT_ACTIONS = {  # our event type -> audit actions that attribute it
    "member_kick": {"kick"}, "member_ban": {"ban"}, "member_unban": {"unban"},
    "role_add": {"member_role_update"}, "role_remove": {"member_role_update"},
    "nick_change": {"member_update"}, "timeout_add": {"member_update", "automod_timeout_member"},
    "timeout_remove": {"member_update"}, "voice_server_mute": {"member_update"},
    "voice_server_unmute": {"member_update"}, "voice_server_deafen": {"member_update"},
    "voice_server_undeafen": {"member_update"},
}


def _diff_text(entry: discord.AuditLogEntry) -> dict:
    out: dict = {}
    try:
        before = dict(iter(entry.changes.before))
        after = dict(iter(entry.changes.after))
    except Exception:  # noqa: BLE001
        return out
    for k in set(before) | set(after):
        b, a = before.get(k), after.get(k)
        if isinstance(a, discord.Permissions) or isinstance(b, discord.Permissions):
            bv = b.value if b else 0
            av = a.value if a else 0
            out[k] = {"added": F.names_of(av & ~bv), "removed": F.names_of(bv & ~av)}
        else:
            conv = lambda v: v.name if hasattr(v, "name") and not isinstance(v, str) else (str(v) if v is not None else None)  # noqa: E731
            if isinstance(b, (list, tuple)) or isinstance(a, (list, tuple)):
                out[k] = {"before": [conv(x) for x in (b or [])], "after": [conv(x) for x in (a or [])]}
            else:
                out[k] = {"before": conv(b), "after": conv(a)}
    return out


class EventLog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.recent_bans: dict[int, datetime] = {}
        self._primed = False

    # ---------------------------------------------------------- utilities
    async def record(self, etype: str, *, guild: discord.Guild, target=None, actor=None, actor_id=None,
                     actor_name=None, confidence="unknown", channel=None, role=None, reason=None,
                     details=None, source="gateway", embed_color=0x95A5A6) -> None:
        via_bot = parse_bot_reason(reason)
        if via_bot:
            actor_id, actor_name, reason = via_bot
            confidence = "confirmed"
            details = {**(details or {}), "via": "the bot"}
            actor = None
        await self.bot.db.add_event(
            type=etype, category=category_of(etype), guild_id=guild.id,
            target_id=getattr(target, "id", None), target_name=getattr(target, "display_name", None) or getattr(target, "name", None),
            actor_id=actor.id if actor else actor_id, actor_name=(getattr(actor, "display_name", None) or getattr(actor, "name", None)) if actor else actor_name,
            actor_confidence=confidence, channel_id=getattr(channel, "id", None), channel_name=getattr(channel, "name", None),
            role_id=getattr(role, "id", None), role_name=getattr(role, "name", None), reason=reason,
            details=details, source=source)
        if etype in self.bot.cfg.post_events:
            desc = [f"**Target:** {getattr(target, 'mention', getattr(target, 'name', '—'))}"]
            if actor or actor_id:
                desc.append(f"**By:** {actor.mention if actor else f'<@{actor_id}>'} ({confidence})")
            elif confidence in ("unknown", "self"):
                desc.append(f"**By:** {confidence}")
            if channel:
                desc.append(f"**Channel:** {getattr(channel, 'mention', channel)}")
            if role:
                desc.append(f"**Role:** @{role.name}")
            if reason:
                desc.append(f"**Reason:** {reason[:500]}")
            await self.bot.post_log(etype, discord.Embed(title=etype.replace("_", " ").title(),
                                                        description="\n".join(desc), color=embed_color,
                                                        timestamp=datetime.now(timezone.utc)))

    def is_target(self, guild: discord.Guild) -> bool:
        g = self.bot.guild
        return g is not None and guild.id == g.id

    async def audit_lookup(self, guild, actions: set[str], target_id: int, now: datetime) -> AuditRec | None:
        await asyncio.sleep(CORRELATE_DELAY)
        rec = self.bot.audit_buffer.find(actions, target_id, now)
        if rec:
            return rec
        # fallback: REST (covers a missed gateway entry)
        try:
            for a in actions:
                action = getattr(discord.AuditLogAction, a, None)
                if action is None:
                    continue
                async for e in guild.audit_logs(limit=6, action=action):
                    if e.target and getattr(e.target, "id", None) == target_id \
                            and abs((now - e.created_at).total_seconds()) < 30 and e.id not in self.bot.audit_buffer.used:
                        self.bot.audit_buffer.used.add(e.id)
                        return AuditRec(e.id, a, e.user_id, getattr(e.user, "name", None), target_id, e.reason, e.created_at)
        except discord.Forbidden:
            pass
        except discord.HTTPException:
            log.warning("audit log fetch failed", exc_info=True)
        return None

    async def _prime_voice(self, guild: discord.Guild) -> None:
        for a in ("member_move", "member_disconnect"):
            try:
                entries = [e async for e in guild.audit_logs(limit=10, action=getattr(discord.AuditLogAction, a))]
                self.bot.voice_attr.prime([{"id": e.id, "count": getattr(e.extra, "count", 1)} for e in entries])
            except discord.HTTPException:
                pass

    # ---------------------------------------------------------- audit log stream
    @commands.Cog.listener()
    async def on_ready(self):
        if not self._primed and self.bot.guild:
            self._primed = True
            await self._prime_voice(self.bot.guild)
            if not await self.bot.db.kv_get("backfill:audit_done"):
                try:
                    n = await self.backfill(self.bot.guild)
                    await self.bot.db.kv_set("backfill:audit_done", {"events": n})
                    log.info("audit-log backfill: %d events imported", n)
                except Exception:  # noqa: BLE001
                    log.exception("audit backfill failed")

    async def backfill(self, guild: discord.Guild, limit: int = 3000) -> int:
        """Import Discord's retained audit log (≈45 days) once, so history/stats start populated.
        Events are marked source=audit_backfill; attribution is exact (from the audit entries)."""
        n = 0
        async for e in guild.audit_logs(limit=limit, oldest_first=True):
            a = e.action.name
            if await self.bot.db.event_exists("audit_backfill", str(e.id)):
                continue
            tgt = e.target
            base = dict(guild_id=guild.id, ts=e.created_at.isoformat(timespec="seconds"), actor_id=e.user_id,
                        actor_name=getattr(e.user, "display_name", None), actor_confidence="confirmed",
                        reason=e.reason, source="audit_backfill")
            tname = getattr(tgt, "display_name", None) or getattr(tgt, "name", None)
            det = {"audit_id": str(e.id)}
            rows: list[dict] = []
            if a in ("kick", "ban", "unban"):
                rows.append({"type": {"kick": "member_kick", "ban": "member_ban", "unban": "member_unban"}[a]})
            elif a == "member_role_update":
                for r in getattr(e.changes.after, "roles", None) or []:
                    rows.append({"type": "role_add", "role_id": r.id, "role_name": getattr(r, "name", None)})
                for r in getattr(e.changes.before, "roles", None) or []:
                    rows.append({"type": "role_remove", "role_id": r.id, "role_name": getattr(r, "name", None)})
            elif a == "member_update":
                ch = _diff_text(e)
                if "timed_out_until" in ch:
                    rows.append({"type": "timeout_add" if ch["timed_out_until"].get("after") not in (None, "None") else "timeout_remove"})
                if "nick" in ch:
                    rows.append({"type": "nick_change", "details": {"old": ch["nick"]["before"], "new": ch["nick"]["after"]}})
                for k, on, off in (("mute", "voice_server_mute", "voice_server_unmute"), ("deaf", "voice_server_deafen", "voice_server_undeafen")):
                    if k in ch:
                        rows.append({"type": on if ch[k].get("after") == "True" else off})
            elif a in ("member_move", "member_disconnect"):
                chan = getattr(e.extra, "channel", None)
                rows.append({"type": "voice_mod_" + a.split("_")[1], "target": False,
                             "channel_id": getattr(chan, "id", None), "channel_name": getattr(chan, "name", None),
                             "details": {"count": getattr(e.extra, "count", 1),
                                         "note": "Discord records who moved/disconnected, not whom"}})
            elif a in STRUCTURAL:
                d = {"changes": _diff_text(e)}
                if a.startswith("overwrite_") and e.extra is not None:
                    d["overwrite_for"] = getattr(e.extra, "name", None) or str(getattr(e.extra, "id", ""))
                chan = tgt if a.startswith(("channel_", "overwrite_")) and not isinstance(tgt, discord.Object) else None
                rows.append({"type": a, "details": d, "channel_id": getattr(chan, "id", None),
                             "channel_name": getattr(chan, "name", None) or (d["changes"].get("name") or {}).get("before")})
            for r in rows:
                has_target = r.pop("target", True)
                extra = r.pop("details", {})
                etype = r.pop("type")
                await self.bot.db.add_event(type=etype, category=category_of(etype), **base,
                                            target_id=getattr(tgt, "id", None) if has_target else None,
                                            target_name=tname if has_target else None, details={**det, **extra}, **r)
                n += 1
        return n

    @commands.Cog.listener()
    async def on_audit_log_entry_create(self, entry: discord.AuditLogEntry):
        if not self.is_target(entry.guild):
            return
        action = entry.action.name
        tgt = entry.target
        self.bot.audit_buffer.add(AuditRec(entry.id, action, entry.user_id, getattr(entry.user, "name", None),
                                           getattr(tgt, "id", None), entry.reason, entry.created_at))
        if action in ("member_move", "member_disconnect"):
            self.bot.voice_attr.observe([{
                "id": entry.id, "action": action, "count": getattr(entry.extra, "count", 1),
                "channel_id": getattr(getattr(entry.extra, "channel", None), "id", None),
                "executor_id": entry.user_id, "executor_name": getattr(entry.user, "name", None),
                "created_at": entry.created_at}], utcnow())
            return
        if action not in STRUCTURAL:
            return
        details = {"changes": _diff_text(entry)}
        channel = role = None
        target_obj = tgt
        if action.startswith("channel_") or action.startswith("overwrite_"):
            channel = tgt if not isinstance(tgt, discord.Object) else discord.Object(tgt.id)
            if action.startswith("overwrite_") and entry.extra is not None:
                ex = entry.extra
                details["overwrite_for"] = getattr(ex, "name", None) or str(getattr(ex, "id", ex))
                if isinstance(ex, discord.Role):
                    role = ex
                target_obj = ex
            if isinstance(channel, discord.Object):
                details["channel_name"] = (details["changes"].get("name") or {}).get("before")
        elif action.startswith("role_"):
            role = tgt if isinstance(tgt, discord.Role) else None
            if role is None:
                details["role_name"] = (details["changes"].get("name") or {}).get("before") or (details["changes"].get("name") or {}).get("after")
        await self.record(action, guild=entry.guild, target=target_obj, actor=entry.user, actor_id=entry.user_id,
                          confidence="confirmed", channel=channel if not isinstance(channel, discord.Object) else None,
                          role=role, reason=entry.reason, details=details, source="audit_log", embed_color=0x9B59B6)

    # ---------------------------------------------------------- membership
    async def member_identity(self, guild: discord.Guild, user) -> dict:
        """Permanent identity record for join/leave/kick/ban. The user ID is the identity; names are context.
        Captured from the cached member at event time; falls back to earlier events when the member wasn't cached."""
        from ..db import LogQuery
        from ..trust import current_tier, tier_role_ids
        is_member = isinstance(user, discord.Member)
        tr = tier_role_ids(self.bot.cfg)
        key = current_tier([r.id for r in user.roles], tr) if is_member and tr else None
        level = guild.get_role(tr[key]).name if key and guild.get_role(tr[key]) else None
        out = {"user_id": user.id, "username": user.name, "global_name": getattr(user, "global_name", None),
               "display_name": getattr(user, "display_name", None), "bot": user.bot,
               "account_created": user.created_at.isoformat(),
               "account_age_days": (utcnow() - user.created_at).days,
               "joined_at": user.joined_at.isoformat() if is_member and user.joined_at else None,
               "trust_level": level, "trust_key": key,
               "roles": [r.name for r in user.roles[1:]] if is_member else None, "member_cached": is_member}
        try:
            rows = await self.bot.db.query_events(LogQuery(guild_id=guild.id, target_id=user.id, limit=200,
                                                           types=["member_join", "invite_used", "guest_join", "tier_change"]))
        except Exception:  # noqa: BLE001
            rows = []
        for r in rows:  # newest first
            if r["type"] in ("invite_used", "guest_join") and "sponsor_name" not in out and r.get("actor_name"):
                out.update(sponsor_name=r["actor_name"], sponsor_id=r.get("actor_id"),
                           sponsor_confidence=r.get("actor_confidence"), via="guest invite" if r["type"] == "guest_join" else "invite")
            if r["type"] == "member_join" and not out["joined_at"]:
                out["joined_at"] = r["ts"]
            if r["type"] == "tier_change" and not out["trust_level"]:
                import json as _json
                d = _json.loads(r.get("details") or "{}")
                k = d.get("to")
                role = guild.get_role(tr[k]) if tr and k in tr else None
                out["trust_key"], out["trust_level"] = k, role.name if role else k
        return out

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if not self.is_target(member.guild):
            return
        await self.record("member_join", guild=member.guild, target=member, confidence="self",
                          details=await self.member_identity(member.guild, member))

    @commands.Cog.listener()
    async def on_raw_member_remove(self, payload: discord.RawMemberRemoveEvent):
        guild = self.bot.get_guild(payload.guild_id)
        if not guild or not self.is_target(guild):
            return
        user = payload.user
        now = utcnow()
        details = await self.member_identity(guild, user)   # captured BEFORE anything else (member state is gone soon)
        await asyncio.sleep(CORRELATE_DELAY + 0.5)
        ban_t = self.recent_bans.get(user.id)
        if ban_t and (utcnow() - ban_t).total_seconds() < 30:
            return  # logged as member_ban
        rec = await self.audit_lookup(guild, {"kick"}, user.id, now)
        if rec:
            await self.record("member_kick", guild=guild, target=user, actor_id=rec.executor_id,
                              actor_name=rec.executor_name or self._name(guild, rec.executor_id),
                              confidence="confirmed", reason=rec.reason, details=details, source="audit_log",
                              embed_color=0xE67E22)
        else:
            # never invent causality: without audit-log access we cannot tell a leave from a removal
            details["cause"] = ("no kick or ban recorded" if guild.me.guild_permissions.view_audit_log
                                else "removal cause unknown (no audit-log access)")
            await self.record("member_leave", guild=guild, target=user, confidence="self", details=details)

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User):
        if not self.is_target(guild):
            return
        self.recent_bans[user.id] = utcnow()
        ident = await self.member_identity(guild, user)
        rec = await self.audit_lookup(guild, {"ban"}, user.id, utcnow())
        await self.record("member_ban", guild=guild, target=user, details=ident,
                          actor_id=rec.executor_id if rec else None,
                          actor_name=(rec.executor_name or self._name(guild, rec.executor_id)) if rec else None,
                          confidence="confirmed" if rec else "unknown", reason=rec.reason if rec else None,
                          source="audit_log" if rec else "gateway", embed_color=0xE74C3C)

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User):
        if not self.is_target(guild):
            return
        rec = await self.audit_lookup(guild, {"unban"}, user.id, utcnow())
        await self.record("member_unban", guild=guild, target=user,
                          actor_id=rec.executor_id if rec else None,
                          actor_name=(rec.executor_name or self._name(guild, rec.executor_id)) if rec else None,
                          confidence="confirmed" if rec else "unknown", reason=rec.reason if rec else None,
                          embed_color=0x2ECC71)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if not self.is_target(after.guild):
            return
        snap = lambda m: {  # noqa: E731
            "nick": m.nick, "role_ids": {r.id for r in m.roles if r.id != m.guild.id},
            "role_names": {r.id: r.name for r in m.roles},
            "timeout_until": m.timed_out_until.isoformat() if m.timed_out_until and m.is_timed_out() else None,
        }
        changes = diff_member(snap(before), snap(after))
        if not changes:
            return
        now = utcnow()
        for etype, det in changes:
            asyncio.create_task(self._attributed_member_event(after, etype, det, now))

    async def _attributed_member_event(self, member, etype, det, now):
        rec = await self.audit_lookup(member.guild, EVENT_ACTIONS[etype], member.id, now)
        # role_add/remove for the same entry: allow re-use (one entry can add several roles)
        if rec is None and etype in ("role_add", "role_remove"):
            rec = self.bot.audit_buffer.find({"member_role_update"}, member.id, now, consume=False)
        role = member.guild.get_role(det["role_id"]) if det.get("role_id") else None
        if rec:
            conf = "self" if rec.executor_id == member.id else "confirmed"
            await self.record(etype, guild=member.guild, target=member, actor_id=rec.executor_id,
                              actor_name=rec.executor_name or self._name(member.guild, rec.executor_id),
                              confidence=conf, role=role, reason=rec.reason, details=det,
                              source="audit_log", embed_color=0xE67E22 if etype == "timeout_add" else 0x95A5A6)
        else:
            conf = "self" if etype == "nick_change" else "unknown"
            if etype == "timeout_remove":
                conf = "unknown"  # natural expiry produces no audit entry
                det = {**det, "note": "no audit entry: probably expired naturally"}
            await self.record(etype, guild=member.guild, target=member, confidence=conf, role=role, details=det)

    # ---------------------------------------------------------- voice
    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if not self.is_target(member.guild):
            return
        vs = lambda s: {"channel_id": s.channel.id if s.channel else None,  # noqa: E731
                        "channel_name": s.channel.name if s.channel else None, "mute": s.mute, "deaf": s.deaf,
                        "self_mute": s.self_mute, "self_deaf": s.self_deaf, "self_stream": bool(s.self_stream),
                        "self_video": s.self_video}
        now = utcnow()
        for etype, det in diff_voice(vs(before), vs(after)):
            asyncio.create_task(self._voice_event(member, etype, det, before, after, now))

    def _session(self, member_id: int, now, start: bool = False, end: bool = False) -> dict:
        """Voice session tracking across moves: join → (moves) → leave."""
        if not hasattr(self, "_voice_since"):
            self._voice_since, self._voice_approx = {}, set()
        if start:
            self._voice_since[member_id] = now
            self._voice_approx.discard(member_id)
            return {}
        if end and member_id in self._voice_since:
            secs = int((now - self._voice_since.pop(member_id)).total_seconds())
            approx = member_id in self._voice_approx
            self._voice_approx.discard(member_id)
            return {"session_seconds": secs, "session_approx": approx}
        return {}

    @commands.Cog.listener("on_ready")
    async def _seed_sessions(self):
        g = self.bot.guild
        if not g:
            return
        now = utcnow()
        self._session(0, now)  # init structures
        for vc in g.voice_channels + g.stage_channels:
            for m in vc.members:
                if m.id not in self._voice_since:
                    self._voice_since[m.id] = now      # already connected before the bot (re)started
                    self._voice_approx.add(m.id)       # → duration is a lower bound

    async def _voice_event(self, member, etype, det, before, after, now):
        g = member.guild
        if etype == "voice_join":
            self._session(member.id, now, start=True)
            await self.record(etype, guild=g, target=member, confidence="self", channel=after.channel, details=det)
            return
        if etype.startswith(("voice_self_", "voice_stream_", "voice_video_")):
            await self.record(etype, guild=g, target=member, confidence="self", channel=after.channel, details=det)
            return
        if etype == "voice_leave":
            det = {**det, **self._session(member.id, now, end=True)}
        if etype == "voice_move" and g.afk_channel and after.channel == g.afk_channel:
            det = {**det, "afk": True}
        if etype in ("voice_leave", "voice_move"):
            action = "member_disconnect" if etype == "voice_leave" else "member_move"
            await asyncio.sleep(1.5)
            try:
                entries = [e async for e in g.audit_logs(limit=5, action=getattr(discord.AuditLogAction, action))]
                self.bot.voice_attr.observe([{
                    "id": e.id, "action": action, "count": getattr(e.extra, "count", 1),
                    "channel_id": getattr(getattr(e.extra, "channel", None), "id", None),
                    "executor_id": e.user_id, "executor_name": getattr(e.user, "name", None),
                    "created_at": e.created_at} for e in entries], utcnow())
            except discord.HTTPException:
                pass
            ex_id, ex_name, conf = self.bot.voice_attr.match(action, det.get("to"), utcnow())
            channel = before.channel if etype == "voice_leave" else after.channel
            if ex_id and ex_id != member.id:
                det = {**det, "attribution": "audit-log correlation (MEMBER_%s has no target; see docs)" % action.split("_")[1].upper()}
                await self.record("voice_disconnect" if etype == "voice_leave" else "voice_move", guild=g, target=member,
                                  actor_id=ex_id, actor_name=ex_name or self._name(g, ex_id), confidence=conf,
                                  channel=channel, details=det, source="audit_log", embed_color=0xE67E22)
            elif det.get("afk"):
                # Discord's idle timeout moves people to AFK without any audit entry
                await self.record("voice_afk", guild=g, target=member, actor_name="Discord idle timeout or self",
                                  confidence="unknown", channel=channel,
                                  details={**det, "attribution": "no moderator action found"})
            else:
                await self.record(etype, guild=g, target=member, confidence="self" if conf == "unknown" else conf,
                                  channel=channel, details={**det, "attribution": "no matching moderator action found"})
            return
        # server mute/deafen: exact via MEMBER_UPDATE with target
        rec = await self.audit_lookup(g, EVENT_ACTIONS[etype], member.id, now)
        await self.record(etype, guild=g, target=member, channel=after.channel,
                          actor_id=rec.executor_id if rec else None,
                          actor_name=(rec.executor_name or self._name(g, rec.executor_id)) if rec else None,
                          confidence="confirmed" if rec else "unknown", reason=rec.reason if rec else None, details=det)

    # ---------------------------------------------------------- messages (metadata only by default)
    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        g = self.bot.guild
        if not g or payload.guild_id != g.id or not self.bot.cfg.message_logging.metadata:
            return
        msg = payload.cached_message
        if msg and msg.author.bot:
            return
        ch = g.get_channel(payload.channel_id)
        det = {"message_id": payload.message_id,
               "created": discord.utils.snowflake_time(payload.message_id).isoformat()}
        if msg and self.bot.cfg.message_logging.content and self.bot.settings.message_content_intent:
            det["content"] = (msg.content or "")[:1000]
            det["attachments"] = len(msg.attachments)
        await self.record("message_delete", guild=g, target=msg.author if msg else None, channel=ch,
                          confidence="unknown", details=det)

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent):
        g = self.bot.guild
        if not g or payload.guild_id != g.id or not self.bot.cfg.message_logging.metadata:
            return
        await self.record("message_bulk_delete", guild=g, channel=g.get_channel(payload.channel_id),
                          details={"count": len(payload.message_ids)})

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent):
        g = self.bot.guild
        if not g or payload.guild_id != g.id or not self.bot.cfg.message_logging.metadata:
            return
        data = payload.data
        author = data.get("author") or {}
        if author.get("bot") or not data.get("edited_timestamp"):
            return  # embeds unfurling etc. are not user edits
        det = {"message_id": payload.message_id}
        if self.bot.cfg.message_logging.content and self.bot.settings.message_content_intent and payload.cached_message:
            det["before"] = (payload.cached_message.content or "")[:1000]
            det["after"] = (data.get("content") or "")[:1000]
        target = g.get_member(int(author["id"])) if author.get("id") else None
        await self.record("message_edit", guild=g, target=target, channel=g.get_channel(payload.channel_id),
                          confidence="self", details=det)

    @staticmethod
    def _name(guild: discord.Guild, uid: int | None) -> str | None:
        if uid is None:
            return None
        m = guild.get_member(uid)
        return m.display_name if m else None


async def setup(bot):
    await bot.add_cog(EventLog(bot))
