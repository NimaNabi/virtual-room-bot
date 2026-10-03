"""Trust tiers vs temporary authority — pure logic (unit-tested).

TRUST (which social/private spaces a friend belongs to): exactly ONE tier role per normal human member.
    tier1 = highest · tier2 · tier3 = default for every normal member. Access is cumulative by the privacy floors,
    so a member never needs more than one tier role.
TEMPORARY AUTHORITY (what administrative action someone may perform, for a limited time): separate roles,
    always expiring, never changing the trust tier.
"""
from __future__ import annotations

from dataclasses import dataclass

TIERS = ("tier1", "tier2", "tier3")          # most private first
DEFAULT_TIER = "tier3"
TIER_TO_TRUST_LEVEL = {"tier1": "trust_level_1", "tier2": "trust_level_2", "tier3": "trust_level_3"}


def tier_role_ids(cfg) -> dict[str, int]:
    """tier key -> role ID, from the configured trust_levels (IDs are canonical; names are display only)."""
    out = {}
    for tier, trust_level in TIER_TO_TRUST_LEVEL.items():
        c = cfg.baseline.trust_levels.get(trust_level)
        if c and c.role.isdigit():
            out[tier] = int(c.role)
    return out


def tiers_held(role_ids, tier_roles: dict[str, int]) -> list[str]:
    return [t for t in TIERS if tier_roles.get(t) in set(role_ids)]


def current_tier(role_ids, tier_roles: dict[str, int]) -> str | None:
    """The most private tier held (stacked roles resolve to the highest trust)."""
    held = tiers_held(role_ids, tier_roles)
    return held[0] if held else None


def plan_set_tier(role_ids, target: str, tier_roles: dict[str, int]) -> tuple[list[int], list[int]]:
    """(add, remove) so the member ends with EXACTLY the target tier role — promotions/demotions replace."""
    if target not in TIERS:
        raise ValueError(f"unknown tier {target}")
    have = set(role_ids)
    add = [tier_roles[target]] if tier_roles[target] not in have else []
    remove = [tier_roles[t] for t in TIERS if t != target and tier_roles.get(t) in have]
    return add, remove


def normalize_plan(role_ids, tier_roles: dict[str, int], *, is_owner_like: bool, is_bot: bool) -> tuple[str | None, list[int], list[int]]:
    """Target tier + (add, remove) that enforces the exactly-one rule. Owners/bots get no tier."""
    if is_bot or is_owner_like:
        return None, [], []
    target = current_tier(role_ids, tier_roles) or DEFAULT_TIER
    add, remove = plan_set_tier(role_ids, target, tier_roles)
    return target, add, remove


# ---------------------------------------------------------------- temporary authority
ELEVATIONS = {
    "mod": {"label": "Temporary Moderator", "default_min": 30, "max_min": 24 * 60},
    "admin": {"label": "Temporary Administrator", "default_min": 10, "max_min": 120},
}
MOD_PERMS = ["moderate_members", "move_members", "mute_members", "deafen_members", "manage_messages",
             "view_channel", "send_messages", "read_message_history", "connect", "speak"]


def clamp_duration(kind: str, minutes: int | None) -> int:
    e = ELEVATIONS[kind]
    if minutes is None or minutes <= 0:
        return e["default_min"]
    return min(int(minutes), e["max_min"])  # never indefinite


@dataclass
class Grant:
    user_id: int
    kind: str
    expires: float
    by: int
    reason: str | None = None

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def due(grants: dict[str, dict], now: float) -> list[Grant]:
    """Grants whose expiry has passed (including ones missed while the bot was down)."""
    return [Grant(**g) for g in grants.values() if g["expires"] <= now]


def grant_key(user_id: int, kind: str) -> str:
    return f"{user_id}:{kind}"


def is_approved_elevation(role_ids_added, actor_id: int, bot_id: int, elevation_role_ids: set[int],
                          active: dict[str, dict], target_id: int) -> bool:
    """A role grant carrying Administrator is legitimate only if the BOT added an elevation role for a user with an
    active bot-recorded grant. Anything else (someone adding the role by hand, other admin roles) stays CRITICAL."""
    added = set(role_ids_added)
    if actor_id != bot_id or not added or not added <= elevation_role_ids:
        return False
    return any(g["user_id"] == target_id for g in active.values())


# ---------------------------------------------------------------- tier capability profiles
# Generic preset (cumulative: each level's role carries the full set; members hold exactly one level role).
# Owners add or remove preferences in config `trust.permissions`; NEVER_FOR_TIERS is enforced regardless.
PRESET = {
    "tier3": ["view_channel", "send_messages", "read_message_history", "attach_files", "embed_links", "add_reactions",
              "use_external_emojis", "use_application_commands", "send_messages_in_threads", "connect", "speak",
              "stream", "use_vad"],
    "tier2": ["use_external_stickers", "create_public_threads", "use_embedded_activities", "use_soundboard"],
    "tier1": ["create_instant_invite", "create_events"],
}
NEVER_FOR_TIERS = ["administrator", "manage_guild", "manage_roles", "manage_channels", "manage_webhooks",
                   "kick_members", "ban_members", "moderate_members", "move_members", "mute_members", "deafen_members",
                   "manage_messages", "manage_threads", "manage_nicknames", "manage_events", "manage_guild_expressions",
                   "mention_everyone", "view_audit_log"]
# every non-destructive social permission the capability matrix compares between levels
SOCIAL_CAPS = ["view_channel", "send_messages", "read_message_history", "attach_files", "embed_links", "add_reactions",
               "use_external_emojis", "use_external_stickers", "send_voice_messages", "send_polls",
               "use_application_commands", "use_external_apps", "send_messages_in_threads", "change_nickname",
               "connect", "speak", "stream", "use_vad", "use_embedded_activities", "use_soundboard",
               "create_public_threads", "use_external_sounds", "set_voice_channel_status", "request_to_speak",
               "create_instant_invite", "create_private_threads", "create_events", "priority_speaker", "bypass_slowmode"]


def tier_permissions(cfg=None) -> dict[str, list[str]]:
    """Cumulative permission lists per level: preset + owner preferences, dangerous permissions always removed."""
    extra = dict(getattr(getattr(cfg, "trust", None), "permissions", {}) or {})
    out, acc = {}, []
    for tier in ("tier3", "tier2", "tier1"):
        acc = acc + PRESET[tier] + list(extra.get(TIER_TO_TRUST_LEVEL[tier], []))
        out[tier] = [p for p in dict.fromkeys(acc) if p not in NEVER_FOR_TIERS]
    return out


TIER_PERMS = tier_permissions()
TIER1_PERMS, TIER2_PERMS, TIER3_PERMS = TIER_PERMS["tier1"], TIER_PERMS["tier2"], TIER_PERMS["tier3"]

# Bot-mediated capabilities (policy, enforced in code): who may use them
BOT_CAPS = {"create_room": ("tier1", "tier2", "tier3"), "music": ("tier1", "tier2", "tier3"),
            "scoped_move": ("tier1", "tier2", "tier3"), "room_controls": ("tier1", "tier2", "tier3"),
            "guest_invite": ("tier1",), "give_standard_access": ("tier1",)}


def capability_matrix(guild, tier_roles: dict[str, int], channel_ids=None) -> dict:
    """Effective permissions of a synthetic member holding ONLY each tier role (no stacking), per channel.
    Returns {"per_tier": {tier: {cap: n_channels}}, "violations": [...]} where a violation is a channel+capability that a
    LOWER tier has but a HIGHER tier lacks (T1 >= T2 >= T3 must hold everywhere)."""
    from .perms import flags as F
    from .perms.engine import compute
    from .perms.model import Member
    syn = {t: Member(-10 - i, f"synthetic-{t}", [rid]) for i, (t, rid) in enumerate(sorted(tier_roles.items()))}
    chans = [c for c in guild.channels.values() if c.type != "category" and (channel_ids is None or c.id in channel_ids)]
    per_tier = {t: {cap: 0 for cap in SOCIAL_CAPS} for t in syn}
    violations = []
    for c in chans:
        eff = {t: compute(guild, m, c).value for t, m in syn.items()}
        for t, v in eff.items():
            for cap in SOCIAL_CAPS:
                if v & F.FLAGS[cap] and v & F.FLAGS["view_channel"]:
                    per_tier[t][cap] += 1
        for hi, lo in (("tier1", "tier2"), ("tier2", "tier3"), ("tier1", "tier3")):
            if hi not in eff or lo not in eff:
                continue
            missing = eff[lo] & ~eff[hi]
            for cap in SOCIAL_CAPS:
                if missing & F.FLAGS[cap]:
                    violations.append({"channel": c.id, "name": c.name, "cap": cap, "higher": hi, "lower": lo})
    return {"per_tier": per_tier, "violations": violations}


def is_prestige_color(color: int) -> bool:
    """Gold / silver / bronze-looking colours read as medals."""
    import colorsys
    if not color:
        return False
    r, g, b = ((color >> 16) & 255) / 255, ((color >> 8) & 255) / 255, (color & 255) / 255
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    hue = h * 360
    if s < 0.18 and l > 0.45:
        return True                                   # silver / grey
    if 38 <= hue <= 60 and s > 0.4:
        return True                                   # gold / yellow
    if 18 <= hue < 38 and l < 0.55 and s > 0.3:
        return True                                   # bronze / copper
    return False


def rel_luminance(color: int) -> float:
    def lin(c):
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * lin((color >> 16) & 255) + 0.7152 * lin((color >> 8) & 255) + 0.0722 * lin(color & 255)
