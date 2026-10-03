"""Guardian classification rules (pure, unit-tested).

Input: an audit-log action + its normalized change set + context. Output: (severity, title) or None
for normal activity that should NOT alert (no spam).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .perms import flags as F

CRITICAL, WARNING, INFO = "CRITICAL", "WARNING", "INFO"
RANK = {CRITICAL: 0, WARNING: 1, INFO: 2}


@dataclass
class Ctx:
    actor: str = "unknown"
    target: str = "?"
    target_is_everyone: bool = False
    target_is_member: bool = False
    target_is_trust_level_role: bool = False
    trusted_bot: bool = False
    role_grants_admin: bool = False          # member_role_update: an added role carries Administrator
    role_grants_dangerous: list[str] = field(default_factory=list)


def _added(changes: dict, key: str) -> list[str]:
    v = changes.get(key) or {}
    return v.get("added", []) if isinstance(v, dict) else []


def _removed(changes: dict, key: str) -> list[str]:
    v = changes.get(key) or {}
    return v.get("removed", []) if isinstance(v, dict) else []


def classify(action: str, changes: dict, ctx: Ctx) -> tuple[str, str] | None:
    a, t = ctx.actor, ctx.target
    if action == "bot_add":
        return (INFO, f"Bot {t} added by {a} (trusted)") if ctx.trusted_bot else (CRITICAL, f"New bot {t} added by {a}")
    if action == "member_prune":
        return CRITICAL, f"Member prune executed by {a}"
    if action in ("integration_create", "webhook_create"):
        return WARNING, f"{action.split('_')[0].title()} created: {t} by {a}"
    if action in ("webhook_update", "webhook_delete", "integration_delete", "integration_update"):
        return INFO, f"{action.replace('_', ' ')}: {t} by {a}"
    if action in ("role_create", "role_update"):
        added = _added(changes, "permissions")
        if "administrator" in added:
            return CRITICAL, f"Administrator granted to role @{t} by {a}"
        danger = [p for p in added if p in F.DANGEROUS]
        if danger:
            return WARNING, f"@{t} gained powerful permissions ({', '.join(danger)}) by {a}"
        removed = _removed(changes, "permissions")
        if removed and ctx.target_is_trust_level_role:
            return WARNING, f"Level role @{t} lost permissions ({', '.join(removed)}) by {a}"
        if action == "role_create":
            return INFO, f"Role @{t} created by {a}"
        return (INFO, f"Role @{t} changed by {a}") if changes else None
    if action == "role_delete":
        return (CRITICAL if ctx.target_is_trust_level_role else WARNING), f"Role @{t} deleted by {a}"
    if action == "channel_delete":
        return WARNING, f"Channel #{t} deleted by {a}"
    if action == "channel_create":
        return INFO, f"Channel #{t} created by {a}"
    if action == "channel_update":
        return INFO, f"Channel #{t} settings changed by {a}"
    if action in ("overwrite_create", "overwrite_update", "overwrite_delete"):
        if ctx.target_is_everyone:
            # private channel exposed: @everyone VIEW deny removed or VIEW allow added
            if "view_channel" in _removed(changes, "deny") or "view_channel" in _added(changes, "allow") \
                    or (action == "overwrite_delete" and "view_channel" in _removed(changes, "deny")):
                return CRITICAL, f"#{t} may have become visible to @everyone (changed by {a})"
            if "view_channel" in _added(changes, "deny"):
                return INFO, f"#{t} hidden from @everyone by {a}"
        if ctx.target_is_member and action == "overwrite_create":
            return WARNING, f"Member-specific permission overwrite added on #{t} by {a}"
        if ctx.target_is_trust_level_role and ("view_channel" in _added(changes, "deny") or "view_channel" in _removed(changes, "allow")):
            return WARNING, f"A Level role lost View Channel on #{t} (changed by {a})"
        danger = [p for p in _added(changes, "allow") if p in F.DANGEROUS]
        if danger:
            return WARNING, f"#{t}: overwrite now allows {', '.join(danger)} (by {a})"
        return INFO, f"Permissions on #{t} changed by {a}"
    if action == "member_role_update":
        if ctx.role_grants_admin:
            return CRITICAL, f"{t} gained Administrator through a role given by {a}"
        if ctx.role_grants_dangerous:
            return WARNING, f"{t} gained {', '.join(ctx.role_grants_dangerous)} through a role given by {a}"
        return None  # ordinary role assignment: logged, not alerted
    if action == "guild_update":
        lowered = [k for k in ("verification_level", "mfa_level", "explicit_content_filter") if k in changes]
        if lowered:
            return WARNING, f"Server security setting changed ({', '.join(lowered)}) by {a}"
        return INFO, f"Server settings changed by {a}"
    if action in ("ban", "kick"):
        return INFO, f"{t} {'banned' if action == 'ban' else 'kicked'} by {a}"
    return None


KIND = {
    "bot_add": "Bot added", "member_prune": "Member prune", "integration_create": "Integration", "webhook_create": "Webhook",
    "role_create": "Role", "role_update": "Role permissions", "role_delete": "Role deleted", "channel_delete": "Channel deleted",
    "channel_create": "Channel", "channel_update": "Channel", "overwrite_create": "Channel permissions",
    "overwrite_update": "Channel permissions", "overwrite_delete": "Channel permissions", "member_role_update": "Role assignment",
    "guild_update": "Server settings", "ban": "Ban", "kick": "Kick",
}
# INFO alerts worth posting to the channel (the rest stay in the database)
IMPORTANT_INFO = {"ban", "kick", "role_create", "role_delete", "channel_create", "channel_delete", "guild_update",
                  "webhook_update", "webhook_delete", "integration_delete", "bot_add"}


def recommend(action: str, sev: str, title: str) -> str | None:
    t = title.lower()
    if "administrator" in t:
        return "If unintended: remove Administrator from that role (or the role from the member). Consider /guardian safemode on while you check."
    if "visible to @everyone" in t:
        return "Check the channel's @everyone overwrite; restore DENY View Channel if it should be private (/permissions channel)."
    if action == "bot_add" and sev == CRITICAL:
        return "Verify who added the bot and why; kick it if unknown. Add trusted bots to guardian.trusted_bots."
    if action == "member_prune":
        return "Check /logs action leave for who was removed."
    if t.startswith("mass ") or "raid" in t:
        return "Consider /guardian safemode on, raising the verification level, and removing the actor's roles."
    if "lost" in t and "trust_level" in t:
        return "Run /baseline check; /baseline repair shows the dry-run fix."
    if "member-specific overwrite" in t:
        return "Prefer role-based access; remove the member overwrite unless it is intentional."
    if action in ("webhook_create", "integration_create"):
        return "Confirm the webhook/integration is expected; delete it if not."
    if "security setting" in t:
        return "Check Server Settings → Safety Setup (verification level / 2FA)."
    return None


class Burst:
    """Detects N events of one kind by the same actor within a window (mass bans, channel deletions...)."""

    def __init__(self, n: int, window_s: float):
        self.n, self.window = n, window_s
        self.hits: dict[tuple, list[datetime]] = {}
        self.fired: dict[tuple, datetime] = {}

    def add(self, key: tuple, t: datetime) -> bool:
        lst = [x for x in self.hits.get(key, []) if (t - x).total_seconds() <= self.window] + [t]
        self.hits[key] = lst
        last = self.fired.get(key)
        if len(lst) >= self.n and (last is None or t - last > timedelta(seconds=self.window)):
            self.fired[key] = t
            return True
        return False


def health_score(findings) -> int:
    """100 minus weighted concrete findings: CRITICAL −20, WARNING −5, INFO −0.5 (INFO capped at −10).
    It summarizes; the findings list is what matters."""
    c = sum(f.severity == CRITICAL for f in findings)
    w = sum(f.severity == WARNING for f in findings)
    i = sum(f.severity == INFO for f in findings)
    return max(0, round(100 - 20 * c - 5 * w - min(10, 0.5 * i)))
