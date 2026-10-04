"""Plain-text rendering of stored events (used by /logs and by the AI tools)."""
from __future__ import annotations

import json

CONF = {"confirmed": "", "likely": " (likely — audit correlation)", "ambiguous": " (ambiguous)",
        "self": " (self)", "unknown": " (actor unknown)"}


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
    target = e.get("target_name") or (f"`{e['target_id']}`" if e.get("target_id") else None)
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
        parts = [parts[0], f"🎟️ New member **{who}** · invited by **{e.get('actor_name') or '?'}**{conf_txt} · "
                           f"{det.get('invite_type', 'guest invite')} · assigned {det.get('assigned', 'default Tier 3')}"
                           + (f" · room **{det['room']}**" if det.get("room") else "")]
        return " ".join(parts)
    if t == "invite_used":
        src = e.get("actor_name") or ("unknown (vanity / discovery / expired)" if not det.get("code") else "?")
        return " ".join([parts[0], f"📥 New member **{who}** · invite `{det.get('code') or '—'}` by **{src}** "
                                   f"({e.get('actor_confidence') or 'unknown'})"])
    if t == "nick_change":
        parts.append(f"'{det.get('old')}' → '{det.get('new')}'")
    if t == "timeout_add" and det.get("until"):
        parts.append(f"until {det['until'][:16]}")
    ch = det.get("changes")
    if isinstance(ch, dict) and ch:
        bits = []
        for k, v in list(ch.items())[:4]:
            if isinstance(v, dict) and ("added" in v or "removed" in v):
                s = []
                if v.get("added"):
                    s.append("+" + ",".join(v["added"]))
                if v.get("removed"):
                    s.append("−" + ",".join(v["removed"]))
                bits.append(f"{k}: {' '.join(s)}")
            elif isinstance(v, dict):
                bits.append(f"{k}: {v.get('before')}→{v.get('after')}")
        if bits:
            parts.append("[" + "; ".join(bits) + "]")
    actor = e.get("actor_name") or (f"`{e['actor_id']}`" if e.get("actor_id") else None)
    conf = e.get("actor_confidence") or "unknown"
    if actor and conf != "self":
        parts.append(f"by **{actor}**{CONF.get(conf, '')}")
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
    "nick_change": "✏️ nickname", "role_add": "➕ role", "role_remove": "➖ role", "zero_pass_grant": "🗝️ Owner area pass",
    "zero_pass_revoke": "🔒 Owner area pass removed", "invite_used": "🎟️ joined via invite", "repair_applied": "🔧 repair", "autoheal": "🩹 auto-heal",
    "safe_mode_on": "🛑 Safe Mode ON", "safe_mode_off": "🟢 Safe Mode OFF", "backup_created": "💾 backup",
}


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
    uid = det.get("user_id") or e.get("target_id")
    name = det.get("display_name") or e.get("target_name") or "—"
    parts = [ts if ts.startswith("<t:") else f"`{ts}`", f"{head}: **{name}**"]
    if det.get("username"):
        parts.append(f"@{det['username']}")
    parts.append(f"· ID `{uid}`")
    if det.get("trust_level"):
        parts.append(f"· level **{det['trust_level']}**")
    if det.get("joined_at") and t != "member_join":
        parts.append(f"· joined {_date(det['joined_at']) or det['joined_at'][:10]}")
    if det.get("account_created"):
        parts.append(f"· account created {_date(det['account_created']) or det['account_created'][:10]}")
    if det.get("sponsor_name"):
        parts.append(f"· joined via **{det['sponsor_name']}**" + (" (guest invite)" if det.get("via") == "guest invite" else ""))
    if t in ("member_kick", "member_ban"):
        parts.append(f"· by **{e['actor_name']}**" if e.get("actor_name") else "· actor unknown")
    elif t == "member_leave" and det.get("cause"):
        parts.append(f"· {det['cause']}")
    if e.get("reason"):
        parts.append(f"— {e['reason'][:120]}")
    return " ".join(parts)


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
    who = e.get("target_name") or "—"
    what = OWNER_ICONS.get(t, t.replace("_", " "))
    if t in ("member_join", "member_leave", "member_kick", "member_ban") and (det.get("user_id") or e.get("target_id")):
        return member_identity_line(ts, t, e, det)
    parts = [f"`{ts}`" if not ts.startswith("<t:") else ts, f"👤 **{who}**", what]
    if t in ("voice_move", "voice_disconnect", "voice_afk", "voice_bot_move") and det.get("from_name"):
        parts.append(f"**{det.get('from_name')}** → **{det.get('to_name') or 'disconnected'}**")
    elif e.get("channel_name"):
        parts.append(f"**{e['channel_name']}**")
    if e.get("role_name"):
        parts.append(f"@{e['role_name']}")
    if t == "nick_change":
        parts.append(f"'{det.get('old')}' → '{det.get('new')}'")
    if det.get("session_seconds") is not None:
        parts.append(f"· session {'≥' if det.get('session_approx') else ''}{_dur(det['session_seconds'])}")
    conf = e.get("actor_confidence")
    if e.get("actor_name") and e.get("actor_id") != e.get("target_id") and conf != "self":
        parts.append(f"· by **{e['actor_name']}**" + (f" ({conf})" if conf not in ("confirmed", None) else ""))
    elif conf == "unknown" and e.get("category") == "moderation":
        parts.append("· actor unknown")
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
