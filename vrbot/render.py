"""Plain-text rendering of stored events (used by /logs and by the AI tools)."""
from __future__ import annotations

import json

CONF = {"confirmed": "", "likely": " (likely — audit correlation)", "ambiguous": " (ambiguous)",
        "self": " (self)", "unknown": " (actor unknown)"}


def permission_lines(t: str, changes: dict) -> list[str]:
    """Readable permission changes (official Discord names) for role and channel-overwrite events."""
    from .perms.flags import readable_changes
    if not isinstance(changes, dict):
        return []
    return readable_changes(changes, in_channel=t.startswith(("overwrite_", "channel_")))


def domain_detail(t: str, det: dict) -> list[str]:
    """Trust/temporary-authority specifics (shared by /logs and the owner log channels)."""
    parts: list[str] = []
    if t == "tier_change":   # our domain: show the actual change, not just that one happened
        lvl = lambda k, n: n or ({"tier1": "Level 1", "tier2": "Level 2", "tier3": "Level 3"}.get(k) or "none")  # noqa: E731
        parts.append(f"{lvl(det.get('from'), det.get('from_name'))} → **{lvl(det.get('to'), det.get('to_name'))}**"
                     + ("" if det.get("verified", True) else " ⚠️ not verified"))
    if t == "elevation_grant":
        kind = "Temporary Admin" if det.get("kind") == "admin" else "Temporary Moderator"
        exp = f" until <t:{int(det['expires'])}:t>" if det.get("expires") else ""
        parts.append(f"**{kind}** for {det.get('minutes', '?')} min{exp}")
    if t == "elevation_revoke":
        parts.append(f"**{'Temporary Admin' if det.get('kind') == 'admin' else 'Temporary Moderator'}** ended"
                     + ("" if det.get("verified_removed", True) else " ⚠️ role still present"))
    return parts


def event_line(e: dict, discord_ts: bool = True) -> str:
    try:
        det = json.loads(e["details"]) if e.get("details") else {}
    except (TypeError, ValueError):
        det = {}
    ts = e["ts"]
    if discord_ts:
        from datetime import datetime
        try:
            ts = f"<t:{int(datetime.fromisoformat(ts).timestamp())}:f>"
        except ValueError:
            pass
    t = e["type"]
    from .identity import label
    who = label(det.get("target_identity"), e.get("target_name"), e.get("target_id")) if e.get("target_id") else None
    target = who if (e.get("target_id") and target_is_person(t, det)) else (e.get("target_name") or who)
    actor_l = label(det.get("actor_identity"), e.get("actor_name"), e.get("actor_id")) if (e.get("actor_name") or e.get("actor_id")) else None
    parts = [ts, f"**{t}**"]
    if target:
        parts.append(target)
    if t == "voice_move" or (t == "voice_disconnect" and det.get("from_name")):
        if det.get("from_name"):
            parts.append(f"#{det.get('from_name')} → #{det.get('to_name')}" if det.get("to_name") else f"from #{det['from_name']}")
    elif e.get("channel_name"):
        parts.append(f"#{e['channel_name']}")
    elif det.get("channel_name"):
        parts.append(f"#{det['channel_name']}")
    if e.get("role_name"):
        parts.append(f"@{e['role_name']}")
    elif det.get("overwrite_for"):
        parts.append(f"for {det['overwrite_for']}")
    elif det.get("role_name"):
        parts.append(f"@{det['role_name']}")
    if t == "guest_join":
        conf_txt = "" if e.get("actor_confidence") in ("confirmed", "likely") else " (attribution ambiguous)"
        return " • ".join([parts[0], f"🎟️ New member {who} · invited by {actor_l or '?'}{conf_txt} · "
                           f"{det.get('invite_type', 'guest invite')} · assigned {det.get('assigned', 'default level')}"
                           + (f" · room **{det['room']}**" if det.get("room") else "")])
    if t == "invite_used":
        src = actor_l or ("unknown (vanity / discovery / expired)" if not det.get("code") else "?")
        return " • ".join([parts[0], f"📥 New member {who} · invite `{det.get('code') or '—'}` by {src} "
                                     f"({e.get('actor_confidence') or 'unknown'})"])
    parts.extend(domain_detail(t, det))
    if t == "nick_change":
        parts.append(f"'{det.get('old')}' → '{det.get('new')}'")
    if t == "timeout_add" and det.get("until"):
        parts.append(f"until {det['until'][:16]}")
    ch = det.get("changes")
    if isinstance(ch, dict) and ch:
        perm_lines = permission_lines(t, ch)
        bits = []
        for k, v in list(ch.items())[:4]:
            if k in ("permissions", "allow", "deny"):
                continue
            if isinstance(v, dict) and ("added" in v or "removed" in v):
                s = []
                if v.get("added"):
                    s.append("+" + ",".join(v["added"]))
                if v.get("removed"):
                    s.append("−" + ",".join(v["removed"]))
                bits.append(f"{k}: {' '.join(s)}")
            elif isinstance(v, dict):
                bits.append(f"{k}: {v.get('before')}→{v.get('after')}")
        if perm_lines:
            bits.append("; ".join(perm_lines))
        if bits:
            parts.append("[" + "; ".join(bits) + "]")
    conf = e.get("actor_confidence") or "unknown"
    if actor_l and conf != "self":
        parts.append(f"by {actor_l}{CONF.get(conf, '')}")
    elif conf in ("unknown",) and t not in ("member_join", "message_delete", "message_bulk_delete"):
        parts.append("by unknown")
    if det.get("via"):
        parts.append("(via the bot)")
    if e.get("reason"):
        parts.append(f"— “{e['reason'][:150]}”")
    return " • ".join(parts)


OWNER_ICONS = {
    "voice_join": "🔊 joined", "voice_leave": "🚪 left voice", "voice_move": "🔀 moved", "voice_disconnect": "⛔ disconnected",
    "voice_afk": "💤 moved to AFK", "voice_self_mute": "🔇 self-muted", "voice_self_unmute": "🎙️ unmuted",
    "voice_self_deafen": "🙉 self-deafened", "voice_self_undeafen": "👂 undeafened", "voice_server_mute": "🔇 server-muted",
    "voice_server_unmute": "🎙️ server-unmuted", "voice_server_deafen": "🙉 server-deafened",
    "voice_server_undeafen": "👂 server-undeafened", "voice_stream_start": "📺 started streaming",
    "voice_stream_stop": "📺 stopped streaming", "voice_video_start": "📷 camera on", "voice_video_stop": "📷 camera off",
    "voice_room_create": "➕ temp room created", "voice_room_delete": "🗑️ temp room deleted",
    "voice_room_transfer": "👑 temp room transferred", "voice_bot_move": "🔀 moved (via bot)",
    "voice_mod_move": "🔀 moderator move", "voice_mod_disconnect": "⛔ moderator disconnect",
    "member_join": "📥 joined the server", "member_leave": "📤 left the server", "member_kick": "👢 kicked",
    "member_ban": "🔨 banned", "member_unban": "♻️ unbanned", "timeout_add": "⏳ timed out", "timeout_remove": "⌛ timeout removed",
    "tier_change": "🧭 trust level changed", "elevation_grant": "⏱️ temporary access granted",
    "elevation_revoke": "⏱️ temporary access ended", "voice_room_trust": "✅ trusted in room", "voice_room_untrust": "➖ untrusted in room",
    "voice_room_block": "🚫 blocked from room", "voice_room_unblock": "♻️ unblocked in room",
    "voice_room_invite": "📨 invited to room", "voice_room_disconnect": "👋 disconnected from room",
    "voice_room_settings": "⚙️ room settings changed", "config_reloaded": "🔁 configuration reloaded",
    "update_requested": "⬆️ update requested", "setup_applied": "🛠️ setup applied",
    "nick_change": "✏️ nickname", "role_add": "➕ role", "role_remove": "➖ role", "zero_pass_grant": "🗝️ Owner area pass",
    "zero_pass_revoke": "🔒 Owner area pass removed", "invite_used": "🎟️ joined via invite", "repair_applied": "🔧 repair", "autoheal": "🩹 auto-heal",
    "safe_mode_on": "🛑 Safe Mode ON", "safe_mode_off": "🟢 Safe Mode OFF", "backup_created": "💾 backup",
    "db_backup": "💾 database backup", "db_backup_failed": "⚠️ database backup FAILED",
    "automod_delete": "🚫 message removed by AutoMod", "automod_config": "🚫 AutoMod settings changed",
}


def downtime_line(det: dict) -> str:
    """⚠️ Bot was offline for 47 min · <from> → <to> · likely reason: … (times render in the viewer's timezone)."""
    from .uptime import REASONS, human_duration
    periods = det.get("periods") or []
    if not periods:
        return "⚠️ Bot was offline (no details)"
    def one(p):
        return (f"**{human_duration(p['seconds'])}** · <t:{int(p['from'])}:f> → <t:{int(p['to'])}:t> · "
                f"likely reason: {REASONS.get(p.get('reason'), p.get('reason'))}")
    if len(periods) == 1:
        return "⚠️ Bot was offline for " + one(periods[0])
    return f"⚠️ Bot was offline {len(periods)} times: " + " | ".join(one(p) for p in periods[:5])


def _dur(s: int) -> str:
    h, rem = divmod(int(s), 3600)
    m, sec = divmod(rem, 60)
    return (f"{h}h " if h else "") + f"{m}m {sec}s"


def _date(iso_ts: str | None, style: str = "D") -> str | None:
    from datetime import datetime
    try:
        return f"<t:{int(datetime.fromisoformat(iso_ts).timestamp())}:{style}>" if iso_ts else None
    except ValueError:
        return None


def member_identity_line(ts: str, t: str, e: dict, det: dict) -> str:
    """Join/leave/kick/ban: the user ID is the permanent identity (mentions stop resolving once someone leaves)."""
    head = {"member_join": "📥 Member joined", "member_leave": "📤 Member left", "member_kick": "👢 Member kicked",
            "member_ban": "🔨 Member banned"}[t]
    from .identity import label as _lab
    uid = det.get("user_id") or e.get("target_id")
    ident = det.get("target_identity") or {"id": uid, "display_name": det.get("display_name"), "username": det.get("username")}
    parts = [ts if ts.startswith("<t:") else f"`{ts}`", f"{head}: {_lab(ident, e.get('target_name'), uid)}"]
    if det.get("trust_level"):
        parts.append(f"· level **{det['trust_level']}**")
    if det.get("joined_at") and t != "member_join":
        parts.append(f"· joined {_date(det['joined_at']) or det['joined_at'][:10]}")
    if det.get("account_created"):
        parts.append(f"· account created {_date(det['account_created']) or det['account_created'][:10]}")
    if det.get("sponsor_name"):
        parts.append(f"· joined via {_lab({'id': det.get('sponsor_id'), 'display_name': det['sponsor_name']})}"
                     + (" (guest invite)" if det.get("via") == "guest invite" else ""))
    if t in ("member_kick", "member_ban"):
        from .identity import label as _l
        parts.append(f"· by {_l(det.get('actor_identity'), e.get('actor_name'), e.get('actor_id'))}"
                     if (e.get("actor_name") or e.get("actor_id")) else "· actor unknown (no audit-log evidence)")
    elif t == "member_leave" and det.get("cause"):
        parts.append(f"· {det['cause']}")
    if e.get("reason"):
        parts.append(f"— {e['reason'][:120]}")
    return " ".join(parts)


PERSON_TARGET = ("voice_", "member_", "nick_", "timeout_", "tier_", "elevation_", "guest_", "standard_access",
                 "invite_used", "zero_pass", "presence_", "role_add", "role_remove", "message_")


def target_is_person(t: str, det: dict) -> bool:
    return bool(det.get("target_identity")) or t.startswith(PERSON_TARGET)


def owner_log_line(e: dict) -> str:
    """Compact, readable one-liner for the owner-only log channels."""
    from datetime import datetime
    try:
        det = json.loads(e["details"]) if e.get("details") else {}
    except (TypeError, ValueError):
        det = {}
    try:
        ts = f"<t:{int(datetime.fromisoformat(e['ts']).timestamp())}:T>"
    except (KeyError, ValueError):
        ts = e.get("ts", "?")
    t = e["type"]
    from .identity import label as _lab
    people = "".join(f" · {k} {_lab(det.get(sk), e.get(nk), e.get(ik))}" for k, ik, sk, nk in (
        ("about", "target_id", "target_identity", "target_name"), ("by", "actor_id", "actor_identity", "actor_name"))
        if e.get(ik))
    if t == "bot_downtime":
        return f"{ts} {downtime_line(det)}{people}"
    if t in ("bot_guild_join", "bot_guild_remove"):
        verb = "➕ Bot was added to" if t == "bot_guild_join" else "➖ Bot was removed from"
        late = " (while it was offline)" if det.get("while_offline") else ""
        size = f" · {det['members']} members" if det.get("members") else ""
        return f"{ts} {verb} server **{det.get('name') or '?'}**{size}{late}{people}"
    if t in ("db_backup", "db_backup_failed"):
        return (f"{ts} {OWNER_ICONS[t]} · {det.get('file', '')} · {det.get('size_kb', '?')} KB{people}"
                if t == "db_backup" else f"{ts} {OWNER_ICONS[t]} · {det.get('error', '?')}{people}")
    who = e.get("target_name") or "—"
    what = OWNER_ICONS.get(t, t.replace("_", " "))
    if t in ("member_join", "member_leave", "member_kick", "member_ban") and (det.get("user_id") or e.get("target_id")):
        return member_identity_line(ts, t, e, det)
    from .identity import label
    tgt = (f"👤 {label(det.get('target_identity'), e.get('target_name'), e.get('target_id'))}"
           if e.get("target_id") and target_is_person(t, det) else f"👤 **{who}**")
    parts = [f"`{ts}`" if not ts.startswith("<t:") else ts, tgt, what]
    if t in ("voice_move", "voice_disconnect", "voice_afk", "voice_bot_move") and det.get("from_name"):
        parts.append(f"**{det.get('from_name')}** → **{det.get('to_name') or 'disconnected'}**")
    elif e.get("channel_name"):
        parts.append(f"**{e['channel_name']}**")
    if e.get("role_name"):
        parts.append(f"@{e['role_name']}")
    if t in ("guest_join", "invite_used"):
        return event_line(e).replace(" • ", " ", 1)
    parts.extend(domain_detail(t, det))
    if t == "nick_change":
        parts.append(f"'{det.get('old')}' → '{det.get('new')}'")
    if det.get("session_seconds") is not None:
        parts.append(f"· session {'≥' if det.get('session_approx') else ''}{_dur(det['session_seconds'])}")
    perm = permission_lines(t, det.get("changes") or {})
    if perm:
        if det.get("overwrite_for"):
            parts.append(f"for **{det['overwrite_for']}**")
        parts.append("· " + " · ".join(perm))
    conf = e.get("actor_confidence")
    if (e.get("actor_name") or e.get("actor_id")) and e.get("actor_id") != e.get("target_id") and conf != "self":
        parts.append(f"· by {label(det.get('actor_identity'), e.get('actor_name'), e.get('actor_id'))}"
                     + (f" ({conf})" if conf not in ("confirmed", None) else ""))
    elif conf == "ambiguous":
        parts.append("· actor unknown (several moderators acted at the same time)")
    elif conf == "unknown" and e.get("category") == "moderation":
        parts.append("· actor unknown")
    if det.get("deleted_by") == "no_moderator_entry":
        parts.append("· no moderator delete recorded (usually the author)")
    if e.get("reason") and e.get("category") != "voice":
        parts.append(f"— {e['reason'][:120]}")
    return " ".join(parts)


def event_dict_for_ai(e: dict) -> dict:
    d = {k: e.get(k) for k in ("ts", "type", "target_name", "target_id", "actor_name", "actor_id",
                               "actor_confidence", "channel_name", "role_name", "reason")}
    try:
        det = json.loads(e["details"]) if e.get("details") else None
    except (TypeError, ValueError):
        det = None
    if det:
        # never hand message text to the model, even when content logging is enabled
        for k in ("content", "before", "after"):
            if isinstance(det.get(k), str):
                det.pop(k)
        d["details"] = det
    return {k: v for k, v in d.items() if v is not None}
