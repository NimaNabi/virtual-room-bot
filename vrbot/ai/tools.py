"""Tools the AI may call. All are READ-ONLY except propose_repair, which only creates a dry-run
proposal; applying it requires a human with repair rights to press a button (normal authz path)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from ..db import LogQuery, iso
from ..perms import flags as F
from ..perms.audit import audit, plan_repair
from ..perms.engine import explain
from ..perms.model import Guild
from ..perms.snapshot import structural_diff
from ..render import event_dict_for_ai

SCHEMAS = [
    {"type": "function", "function": {
        "name": "find_member", "description": "Resolve a member name/nickname/ID to members (id, name, roles, trust_level).",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "list_channels", "description": "List channels with id, type and category.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "search_logs",
        "description": "Search stored server events. Types include member_join, member_leave, member_kick, member_ban, "
                       "member_unban, timeout_add, timeout_remove, nick_change, role_add, role_remove, voice_join, "
                       "voice_leave, voice_move, voice_disconnect, voice_server_mute, voice_server_deafen, channel_update, "
                       "overwrite_update, role_update, mod_warn, mod_action, message_delete. actor_confidence tells how "
                       "certain the 'who did it' is (confirmed / likely / ambiguous / self / unknown).",
        "parameters": {"type": "object", "properties": {
            "user": {"type": "string", "description": "member name or id; matches target OR actor"},
            "target": {"type": "string", "description": "member the action happened to"},
            "actor": {"type": "string", "description": "member who performed actions"},
            "types": {"type": "array", "items": {"type": "string"}},
            "category": {"type": "string", "enum": ["membership", "voice", "moderation", "structure", "message", "security", "bot"]},
            "since_hours": {"type": "number"}, "text": {"type": "string"},
            "limit": {"type": "integer", "default": 30}}}}},
    {"type": "function", "function": {
        "name": "explain_permission",
        "description": "Exact Discord permission calculation for a member in a channel with reasons and recommended fix.",
        "parameters": {"type": "object", "properties": {
            "member": {"type": "string"}, "channel": {"type": "string"},
            "permission": {"type": "string", "description": "optional, e.g. send_messages, view_channel, connect, speak"}},
            "required": ["member", "channel"]}}},
    {"type": "function", "function": {
        "name": "channel_access",
        "description": "Who can see a channel: its privacy floor, which Trust levels/roles can view it, and how many members.",
        "parameters": {"type": "object", "properties": {"channel": {"type": "string"}}, "required": ["channel"]}}},
    {"type": "function", "function": {
        "name": "permission_problems", "description": "Run the permission audit (server doctor). Returns findings by severity.",
        "parameters": {"type": "object", "properties": {"trust_level": {"type": "string"},
                                                        "include_info": {"type": "boolean", "default": False}}}}},
    {"type": "function", "function": {
        "name": "changes_since", "description": "Structural changes (roles, channels, overwrites) since N hours ago, from snapshots + audit log events.",
        "parameters": {"type": "object", "properties": {"hours": {"type": "number", "default": 24}}}}},
    {"type": "function", "function": {
        "name": "server_status", "description": "Member counts, who is in voice, 7-day activity counts.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "propose_repair",
        "description": "Create a DRY-RUN plan to repair drift from the Level baseline. Does NOT change anything; "
                       "an authorized human must press Apply.",
        "parameters": {"type": "object", "properties": {"trust_level": {"type": "string"}}}}},
]


def _fuzzy(items, query, key):
    q = query.lower().strip().lstrip("@#")
    exact = [i for i in items if key(i).lower() == q]
    return exact or [i for i in items if q in key(i).lower()]


OWNER_ONLY_TOOLS = {"permission_problems", "changes_since", "propose_repair"}
NEUTRAL_ACCESS = "Access to private areas is assigned individually by the server owner."


def schemas_for(owner_view: bool) -> list[dict]:
    """Non-owners are never even offered owner tools."""
    return SCHEMAS if owner_view else [s for s in SCHEMAS if s["function"]["name"] not in OWNER_ONLY_TOOLS]


class Tools:
    """Read-only tools. Privacy is enforced HERE, before anything reaches the model: a non-owner viewer can only
    resolve channels they can see, and only gets their own voice/membership history in those channels."""

    def __init__(self, bot, viewer_id: int | None = None):
        from ..perms.privacy import visible_channel_ids
        self.bot = bot
        self.proposal = None  # last plan proposed during this conversation
        self.used: list[str] = []
        g = bot.guild
        self.viewer_id = viewer_id
        self.owner_view = viewer_id is None or bot.cfg.privacy.is_owner_like(viewer_id, g.owner_id if g else None)
        self.visible: set[int] | None = None if self.owner_view else visible_channel_ids(bot.model(), viewer_id)

    def _model(self) -> Guild:
        return self.bot.model()

    def _member(self, ref: str):
        g = self.bot.guild
        ref = ref.strip().strip("<@!>")
        if ref.isdigit():
            m = g.get_member(int(ref))
            return [m] if m else []
        cands = {m.id: m for m in _fuzzy(g.members, ref, lambda m: m.display_name)}
        cands.update({m.id: m for m in _fuzzy(g.members, ref, lambda m: m.name)})
        return list(cands.values())

    def _channel(self, ref: str):
        g = self.bot.guild
        ref = ref.strip().strip("<#>")
        pool = g.channels if self.owner_view else [c for c in g.channels if c.id in self.visible]
        if ref.isdigit():
            c = next((c for c in pool if c.id == int(ref)), None)
            return [c] if c else []
        return _fuzzy(pool, ref, lambda c: c.name)

    def _uid(self, ref: str | None):
        if not ref:
            return None, None
        ms = self._member(ref)
        if len(ms) == 1:
            return ms[0].id, None
        if ref.strip().isdigit():
            return int(ref), None  # may have left the server
        return None, {"error": f"'{ref}' matched {len(ms)} members", "candidates": [m.display_name for m in ms[:10]]}

    async def call(self, name: str, args: dict) -> dict:
        self.used.append(name)
        fn = getattr(self, f"t_{name}", None)
        if not self.owner_view and name in OWNER_ONLY_TOOLS:
            return {"error": "This information is only available to the server owner."}
        if fn is None:
            return {"error": f"unknown tool {name}"}
        try:
            return await fn(**args)
        except TypeError as e:
            return {"error": f"bad arguments: {e}"}
        except Exception as e:  # noqa: BLE001
            return {"error": f"{type(e).__name__}: {e}"}

    # ---------------------------------------------------------- tools
    async def t_find_member(self, query: str):
        if not self.owner_view:  # names only: no roles, no tier, presence only in rooms the viewer can see
            return {"members": [{"id": m.id, "name": m.display_name,
                                 "in_voice": m.voice.channel.name if m.voice and m.voice.channel and m.voice.channel.id in self.visible else None}
                                for m in self._member(query)[:10]]}
        trust_levels = {rid: k for k, rid in self.bot.trust_level_role_ids().items()}
        return {"members": [{"id": m.id, "name": m.display_name, "username": m.name,
                             "roles": [r.name for r in m.roles[1:]],
                             "trust_level": next((trust_levels[r.id] for r in m.roles if r.id in trust_levels), None),
                             "in_voice": m.voice.channel.name if m.voice and m.voice.channel else None}
                            for m in self._member(query)[:10]]}

    async def t_list_channels(self):
        chans = [c for c in self.bot.guild.channels if self.owner_view or c.id in self.visible]
        return {"channels": [{"id": c.id, "name": c.name, "type": str(c.type),
                              "category": c.category.name if c.category else None} for c in chans]}

    async def t_search_logs(self, user=None, target=None, actor=None, types=None, category=None, since_hours=None,
                            text=None, limit=30):
        if not self.owner_view:
            from ..perms.privacy import filter_events_for_viewer
            q = LogQuery(guild_id=self.bot.guild.id, target_id=self.viewer_id, types=types or [], limit=200)
            if since_hours:
                q.since = iso(datetime.now(timezone.utc) - timedelta(hours=float(since_hours)))
            rows = filter_events_for_viewer(await self.bot.db.query_events(q), self.visible, self.viewer_id)[:60]
            return {"count": len(rows), "events": [event_dict_for_ai(r) for r in rows],
                    "note": "Only your own voice/membership history in areas you can access is available."}
        q = LogQuery(guild_id=self.bot.guild.id, types=types or [], category=category, text=text,
                     limit=min(int(limit or 30), 80))
        for field, ref in (("user_id", user), ("target_id", target), ("actor_id", actor)):
            uid, err = self._uid(ref)
            if err:
                return err
            setattr(q, field, uid)
        if since_hours:
            q.since = iso(datetime.now(timezone.utc) - timedelta(hours=float(since_hours)))
        rows = await self.bot.db.query_events(q)
        return {"count": len(rows), "events": [event_dict_for_ai(r) for r in rows],
                "note": "Only events recorded since the bot joined are available."}

    async def t_explain_permission(self, member: str, channel: str, permission: str | None = None):
        ms, cs = self._member(member), self._channel(channel)
        if not self.owner_view:
            if len(ms) != 1 or ms[0].id != self.viewer_id:
                return {"error": "You can only check your own access. " + NEUTRAL_ACCESS}
            if len(cs) != 1:
                return {"answer": "You don't have access to that area.", "note": NEUTRAL_ACCESS}
        if len(ms) != 1:
            return {"error": f"member '{member}' matched {len(ms)}", "candidates": [m.display_name for m in ms[:10]]}
        if len(cs) != 1:
            return {"error": f"channel '{channel}' matched {len(cs)}", "candidates": [c.name for c in cs[:10]]}
        g = self._model()
        ex = explain(g, g.members[ms[0].id], g.channels[cs[0].id], [permission] if permission else None)
        return {"member": ms[0].display_name, "channel": cs[0].name,
                "results": [{"permission": t.perm, "allowed": t.allowed, "reason": t.reason, "steps": t.steps,
                             "recommended_fix": t.fix} for t in ex.traces], "notes": ex.notes}

    async def t_channel_access(self, channel: str):
        from ..perms.privacy import area_tier, member_tier
        cs = self._channel(channel)
        if not self.owner_view:
            if len(cs) != 1:
                return {"answer": "You don't have access to that area.", "note": NEUTRAL_ACCESS}
            return {"channel": cs[0].name, "you_have_access": True}
        if len(cs) != 1:
            return {"error": f"channel '{channel}' matched {len(cs)}", "candidates": [c.name for c in cs[:10]]}
        m = self._model()
        ch = m.channels[cs[0].id]
        p = self.bot.cfg.privacy
        from ..perms import flags as F2
        from ..perms.engine import compute as comp
        viewers = [mm for mm in m.members.values() if not mm.bot and comp(m, mm, ch).value & F2.FLAGS["view_channel"]]
        by_floor: dict = {}
        for mm in viewers:
            k = "owner" if mm.id == p.owner_id else member_tier(p, set(mm.role_ids))
            by_floor[k] = by_floor.get(k, 0) + 1
        return {"channel": ch.name, "floor": area_tier(m, p, ch), "members_who_can_view": len(viewers),
                "viewers_by_floor": by_floor, "note": "Owner area and owner logs are owner + bot only by design."}

    async def t_permission_problems(self, trust_level=None, include_info=False):
        fs = audit(self._model(), self.bot.cfg, self.bot.user.id, list(self.bot.bot_owner_ids()))
        fs = [f for f in fs if (include_info or f.severity != "INFO") and (trust_level is None or f.trust_level == trust_level)]
        return {"findings": [{"severity": f.severity, "title": f.title, "detail": f.detail, "auto_fixable": f.fixable}
                             for f in fs[:60]], "total": len(fs)}

    async def t_changes_since(self, hours=24):
        g = self.bot.guild
        since = iso(datetime.now(timezone.utc) - timedelta(hours=float(hours)))
        snap = await self.bot.db.latest_snapshot(g.id, before_ts=since)
        diff = structural_diff(Guild.from_dict(snap["data"]), self._model()) if snap else None
        rows = await self.bot.db.query_events(LogQuery(guild_id=g.id, category="structure", since=since, limit=60))
        return {"snapshot_baseline": snap["ts"] if snap else None,
                "structural_diff": diff if diff is not None else "no snapshot older than the window",
                "audit_events": [event_dict_for_ai(r) for r in rows]}

    async def t_server_status(self):
        g = self.bot.guild
        if not self.owner_view:
            return {"members": g.member_count,
                    "voice": {vc.name: len(vc.members) for vc in g.voice_channels if vc.members and vc.id in self.visible}}
        since = iso(datetime.now(timezone.utc) - timedelta(days=7))
        return {"members": g.member_count, "humans": sum(not m.bot for m in g.members),
                "voice": {vc.name: [m.display_name for m in vc.members] for vc in g.voice_channels if vc.members},
                "last_7_days": await self.bot.db.count_events(since, g.id)}

    async def t_propose_repair(self, trust_level=None):
        plan = plan_repair(self._model(), self.bot.cfg, self.bot.user.id, scope=trust_level)
        self.proposal = (plan, trust_level)
        return {"dry_run": True, "changes": [c.describe() for c in plan.changes],
                "resolves": [f.title for f in plan.resolved], "would_introduce": [f.title for f in plan.introduced],
                "blocked": plan.blocked,
                "next_step": "An Apply button is shown to the user; only owners/admins with repair rights can apply."}


def dumps(obj) -> str:
    s = json.dumps(obj, default=str)
    return s if len(s) < 14000 else s[:14000] + '..."(truncated)"'
