"""Event normalization and audit-log correlation.

Discord facts this relies on (docs.discord.com/developers/resources/audit-log):
* Kicks, bans, role changes, nick/timeout/server-mute/deafen changes produce audit entries WITH target_id
  -> attribution is exact ("confirmed").
* MEMBER_MOVE (26) and MEMBER_DISCONNECT (27) entries have NO target_id; they carry executor, count and
  (for moves) the destination channel. Discord also aggregates repeated actions by the same moderator
  into one entry by bumping `count`. So voice move/disconnect attribution is a correlation ("likely").
* A plain voice leave with no matching entry is logged as self/unknown — we never invent an actor.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

CATEGORY = {
    "config_reloaded": "security", "update_requested": "security", "setup_applied": "security",
    "voice_room_settings": "voice", "voice_room_trust": "voice", "voice_room_untrust": "voice", "voice_room_block": "voice",
    "voice_room_unblock": "voice", "voice_room_invite": "voice", "voice_room_disconnect": "voice",
    "member_join": "membership", "member_leave": "membership", "member_kick": "moderation",
    "member_ban": "moderation", "member_unban": "moderation", "timeout_add": "moderation",
    "timeout_remove": "moderation", "nick_change": "membership", "role_add": "membership",
    "role_remove": "membership", "voice_join": "voice", "voice_leave": "voice", "voice_move": "voice",
    "voice_disconnect": "voice", "voice_server_mute": "voice", "voice_server_unmute": "voice",
    "voice_server_deafen": "voice", "voice_server_undeafen": "voice",
    "message_delete": "message", "message_edit": "message", "message_bulk_delete": "message",
    "mod_action": "moderation", "mod_warn": "moderation", "raid_alert": "security",
    "account_age_warning": "security", "repair_applied": "bot", "autoheal": "bot", "backup_created": "bot",
    "voice_mod_move": "voice", "voice_mod_disconnect": "voice", "guardian_alert": "security",
    "voice_self_mute": "voice", "voice_self_unmute": "voice", "voice_self_deafen": "voice", "voice_self_undeafen": "voice",
    "voice_stream_start": "voice", "voice_stream_stop": "voice", "voice_video_start": "voice", "voice_video_stop": "voice",
    "voice_afk": "voice", "voice_room_transfer": "voice", "zero_pass_grant": "security", "zero_pass_revoke": "security",
    "voice_bot_move": "voice", "invite_used": "membership",
    "command_denied": "security", "safe_mode_on": "security", "safe_mode_off": "security",
    "bot_started": "bot", "ai_query": "bot", "voice_room_create": "voice", "voice_room_delete": "voice",
    "bot_downtime": "bot", "db_backup": "bot", "db_backup_failed": "bot", "bot_guild_join": "bot",
    "bot_guild_remove": "bot", "automod_delete": "moderation", "automod_config": "security",
}


def category_of(event_type: str) -> str:
    if event_type in CATEGORY:
        return CATEGORY[event_type]
    if event_type.startswith(("channel_", "overwrite_", "role_", "guild_", "emoji_", "sticker_", "webhook_",
                              "invite_", "integration_", "thread_", "automod_", "stage_", "event_", "bot_")):
        return "structure"
    return "other"


# ---------------------------------------------------------------- diffs
def diff_voice(before: dict, after: dict) -> list[tuple[str, dict]]:
    """before/after: {'channel_id','channel_name','mute','deaf'} (server mute/deaf, not self)."""
    out: list[tuple[str, dict]] = []
    bc, ac = before.get("channel_id"), after.get("channel_id")
    if bc is None and ac is not None:
        out.append(("voice_join", {"to": ac, "to_name": after.get("channel_name")}))
    elif bc is not None and ac is None:
        out.append(("voice_leave", {"from": bc, "from_name": before.get("channel_name")}))
    elif bc != ac:
        out.append(("voice_move", {"from": bc, "from_name": before.get("channel_name"),
                                   "to": ac, "to_name": after.get("channel_name")}))
    # State changes only while connected (a persisted mute on join is not a new action).
    if bc is not None and ac is not None:
        for key, on, off in (("mute", "voice_server_mute", "voice_server_unmute"),
                             ("deaf", "voice_server_deafen", "voice_server_undeafen"),
                             ("self_mute", "voice_self_mute", "voice_self_unmute"),
                             ("self_deaf", "voice_self_deafen", "voice_self_undeafen"),
                             ("self_stream", "voice_stream_start", "voice_stream_stop"),
                             ("self_video", "voice_video_start", "voice_video_stop")):
            if key in before and key in after and bool(before.get(key)) != bool(after.get(key)):
                out.append((on if after.get(key) else off, {}))
    return out


def diff_member(before: dict, after: dict) -> list[tuple[str, dict]]:
    """before/after: {'nick', 'role_ids': set, 'role_names': dict id->name, 'timeout_until': iso|None}"""
    out: list[tuple[str, dict]] = []
    if before.get("nick") != after.get("nick"):
        out.append(("nick_change", {"old": before.get("nick"), "new": after.get("nick")}))
    br, ar = set(before.get("role_ids", ())), set(after.get("role_ids", ()))
    names = {**before.get("role_names", {}), **after.get("role_names", {})}
    for rid in sorted(ar - br):
        out.append(("role_add", {"role_id": rid, "role_name": names.get(rid)}))
    for rid in sorted(br - ar):
        out.append(("role_remove", {"role_id": rid, "role_name": names.get(rid)}))
    bt, at = before.get("timeout_until"), after.get("timeout_until")
    if bt != at:
        if at:
            out.append(("timeout_add", {"until": at}))
        elif bt:
            out.append(("timeout_remove", {"was_until": bt}))
    return out


# accepts the legacy "[ServerBot: ...]" tag too, so older audit history still parses
_BOT_REASON = re.compile(r"\[(?:bot|Bot): (?P<name>.+?) \((?P<id>\d+)\)\]\s*(?P<reason>.*)$", re.S)


def bot_reason(actor_name: str, actor_id: int, reason: str | None) -> str:
    """Audit-log reason used for actions the bot performs on behalf of a human."""
    return f"[bot: {actor_name} ({actor_id})] {reason or 'no reason given'}"[:512]


def parse_bot_reason(reason: str | None) -> tuple[int, str, str] | None:
    if not reason:
        return None
    m = _BOT_REASON.match(reason)
    if not m:
        return None
    return int(m["id"]), m["name"], m["reason"]


# ---------------------------------------------------------------- audit buffer
@dataclass
class AuditRec:
    id: int
    action: str           # discord.AuditLogAction name, e.g. "kick", "member_role_update"
    executor_id: int | None
    executor_name: str | None
    target_id: int | None
    reason: str | None
    created_at: datetime
    extra: dict = field(default_factory=dict)


class AuditBuffer:
    """Short-lived memory of audit entries received via GUILD_AUDIT_LOG_ENTRY_CREATE."""

    def __init__(self, ttl: float = 120.0):
        self.ttl = ttl
        self.items: list[AuditRec] = []
        self.used: set[int] = set()

    def add(self, rec: AuditRec) -> None:
        self.items.append(rec)
        self._gc(rec.created_at)

    def _gc(self, now: datetime) -> None:
        cutoff = now - timedelta(seconds=self.ttl)
        self.items = [i for i in self.items if i.created_at >= cutoff]

    def find(self, actions: set[str], target_id: int, now: datetime, within: float = 20.0,
             consume: bool = True) -> AuditRec | None:
        for rec in reversed(self.items):
            if rec.action in actions and rec.target_id == target_id and rec.id not in self.used \
                    and abs((now - rec.created_at).total_seconds()) <= within:
                if consume:
                    self.used.add(rec.id)
                return rec
        return None


class VoiceAttributor:
    """Correlates aggregated audit entries (count-based) with gateway events: MEMBER_MOVE / MEMBER_DISCONNECT
    (no target at all) and MESSAGE_DELETE (target = author, plus channel).

    Discord merges repeated actions by the same moderator into ONE entry and bumps its `count`. So each observed
    increase of an entry's count becomes that many 'credits'; an event consumes one matching credit. Entries seen
    during priming never produce credits (they are history). If credits from DIFFERENT executors match, nobody is
    named: a wrong accusation is worse than "actor unknown".
    """

    def __init__(self, window: float = 45.0):
        self.window = window
        self.seen: dict[int, int] = {}
        self.credits: list[dict] = []

    def prime(self, entries: list[dict]) -> None:
        for e in entries:
            self.seen[e["id"]] = int(e.get("count") or 1)

    def observe(self, entries: list[dict], now: datetime) -> None:
        for e in entries:
            count = int(e.get("count") or 1)
            prev = self.seen.get(e["id"])
            if prev is None:
                # new entry: only credit if it is fresh; it may also be an aggregate updated later
                age = (now - e["created_at"]).total_seconds()
                delta = count if age <= self.window else 0
            else:
                delta = max(0, count - prev)
            self.seen[e["id"]] = count
            if delta:
                self.credits.append({"entry": e["id"], "action": e["action"], "executor_id": e["executor_id"],
                                     "executor_name": e.get("executor_name"), "channel_id": e.get("channel_id"),
                                     "target_id": e.get("target_id"), "n": delta, "at": now})
        cutoff = now - timedelta(seconds=self.window)
        self.credits = [c for c in self.credits if c["at"] >= cutoff and c["n"] > 0]

    def match(self, action: str, channel_id: int | None, now: datetime,
              target_id: int | None = None) -> tuple[int | None, str | None, str]:
        """Return (executor_id, executor_name, confidence), confidence in likely|ambiguous|unknown.
        'ambiguous' never names an executor and consumes nothing (we cannot know whose credit it was)."""
        def fits(c):
            if c["action"] != action:
                return False
            if action in ("member_move", "message_delete") and c["channel_id"] not in (None, channel_id):
                return False
            if c.get("target_id") is not None and c["target_id"] != target_id:
                return False
            return True
        cands = [c for c in self.credits if fits(c)]
        if not cands:
            return None, None, "unknown"
        if len({c["executor_id"] for c in cands}) > 1:
            return None, None, "ambiguous"
        c = cands[0]
        c["n"] -= 1
        self.credits = [x for x in self.credits if x["n"] > 0]
        return c["executor_id"], c["executor_name"], "likely"


class JoinRate:
    """Sliding-window join counter for raid detection. Alerts once per burst."""

    def __init__(self, threshold: int, window_s: float):
        self.threshold, self.window = threshold, window_s
        self.times: list[datetime] = []
        self.alerted_until: datetime | None = None

    def add(self, t: datetime) -> bool:
        self.times = [x for x in self.times if (t - x).total_seconds() <= self.window] + [t]
        if len(self.times) >= self.threshold and (self.alerted_until is None or t > self.alerted_until):
            self.alerted_until = t + timedelta(seconds=self.window)
            return True
        return False


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
