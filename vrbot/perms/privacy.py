"""Privacy tiers ("floors"): owner-only Owner area → Level 1 → Level 2 → Level 3 → public.

Pure functions on model.Guild + config.PrivacyCfg. Tier-derived expectations feed the normal auditor
(so Permission Doctor, baseline, Guardian drift and auto-heal all enforce the same model), and
privacy_audit() proves nobody can see a floor above their own.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import flags as F
from .engine import compute
from .model import Channel, Guild, Member

VOICE_POWERS = ("move_members", "mute_members", "deafen_members")
TEXT_READ = ("view_channel", "read_message_history")


def area_tier(guild: Guild, privacy, ch: Channel) -> str | None:
    if str(ch.id) in privacy.areas:
        return privacy.areas[str(ch.id)]
    if ch.parent_id and str(ch.parent_id) in privacy.areas:
        return privacy.areas[str(ch.parent_id)]
    return None


def member_tier(privacy, role_ids) -> str:
    """Most private tier among the member's roles (Level 1 + Level 3 = Level 1)."""
    best = privacy.public_key
    for key, t in privacy.tiers.items():
        if any(int(r) in role_ids for r in t.roles) and t.rank < privacy.tiers[best].rank:
            best = key
    return best


def tier_expectations(guild: Guild, privacy) -> dict[int, dict[str, dict[str, bool]]]:
    """channel_id -> tier_key ('default' for public) -> expected perms. Allowed tiers get full use,
    others get View=NO (invisibility, not just Connect=NO)."""
    out: dict[int, dict[str, dict[str, bool]]] = {}
    if not privacy.tiers:
        return out
    pub = privacy.public_key
    for ch in guild.channels.values():
        at = area_tier(guild, privacy, ch)
        if at is None:
            continue
        need = privacy.tiers[at].rank
        for key, t in privacy.tiers.items():
            if not t.roles and key != pub:
                continue  # Owner area has no role: only the owner (+ bot); nothing to express per role
            label = "default" if key == pub else key
            if t.rank <= need:
                if ch.type == "category":
                    exp = {"view_channel": True}
                elif ch.is_voice:
                    exp = {"view_channel": True, "connect": True, "speak": True}
                else:
                    exp = {"view_channel": True, "send_messages": True, "read_message_history": True}
            else:
                exp = {"view_channel": False}
            out.setdefault(ch.id, {})[label] = exp
    return out


@dataclass
class Leak:
    severity: str
    code: str
    title: str
    detail: str
    key: tuple


def privacy_audit(guild: Guild, cfg, bot_id: int | None, allowed_member_overwrites: set[tuple[int, int]] = frozenset()) -> list[Leak]:
    p = cfg.privacy
    out: list[Leak] = []
    if not p.tiers:
        return out
    owner = p.owner_id or guild.owner_id
    owner_like = lambda uid: p.is_owner_like(uid, guild.owner_id)  # noqa: E731
    pub = p.public_key
    # 1) every real member vs every classified area
    temp_admins = set(guild.settings.get("temporary_admins", []))
    for m in guild.members.values():
        if m.bot or owner_like(m.id) or m.id in temp_admins:
            continue  # temporary admins: reported once as INFO below (bot-managed, expiring)
        mt = member_tier(p, m.role_ids)
        mrank = p.tiers[mt].rank
        admin = compute(guild, m).admin
        for ch in guild.channels.values():
            at = area_tier(guild, p, ch)
            if at is None or p.tiers[at].rank >= mrank:
                continue
            v = compute(guild, m, ch).value
            if v & F.FLAGS["view_channel"]:
                why = "Administrator bypasses every overwrite" if admin else (
                    "member-specific overwrite" if ch.overwrites.get(m.id) else "role/overwrite combination")
                if (ch.id, m.id) in allowed_member_overwrites and not admin:
                    continue  # active, bot-managed temporary pass (e.g. /owner_room bring)
                out.append(Leak("CRITICAL", "privacy_leak",
                                f"{m.name} ({mt}) can see {ch.mention} ({at})",
                                f"Cause: {why}. Text chat and presence of that room are visible to them.",
                                ("privacy_leak", ch.id, m.id)))
    # 2) synthetic tier members incl. multi-role combinations
    tier_roles = {k: [int(r) for r in t.roles] for k, t in p.tiers.items() if t.roles}
    combos = [[r] for rs in tier_roles.values() for r in rs[:1]]
    keys = list(tier_roles)
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            combos.append([tier_roles[a][0], tier_roles[b][0]])
    combos.append([])
    for roles in combos:
        mt = member_tier(p, set(roles))
        sm = Member(-2, f"synthetic({mt})", roles)
        for ch in guild.channels.values():
            at = area_tier(guild, p, ch)
            if at is None:
                continue
            can = bool(compute(guild, sm, ch).value & F.FLAGS["view_channel"])
            should = p.tiers[at].rank >= p.tiers[mt].rank
            if can and not should:
                out.append(Leak("CRITICAL", "privacy_tier_leak", f"Tier {mt} can see {ch.mention} ({at})",
                                f"roles {roles}", ("privacy_tier_leak", ch.id, tuple(roles))))
            elif should and not can and not (ch.type == "category"):
                out.append(Leak("WARNING", "privacy_tier_blocked", f"Tier {mt} cannot see {ch.mention} ({at})",
                                f"roles {roles}: expected access to its own and lower floors", ("privacy_tier_blocked", ch.id, tuple(roles))))
    # 3) Administrators bypass every View deny (including Owner area and owner logs)
    for m in guild.members.values():
        if m.bot or m.id == owner or not compute(guild, m).admin:
            continue
        if m.id in set(guild.settings.get("temporary_admins", [])):
            out.append(Leak("INFO", "privacy_temporary_admin", f"Temporary Administrator (bot-managed, expiring): {m.name}",
                            "Granted with /access elevate; revoked automatically at expiry.", ("temp_admin", m.id)))
        elif m.id in p.approved_owner_equivalents:
            out.append(Leak("INFO", "privacy_owner_equivalent", f"Approved owner-equivalent: {m.name}",
                            "Explicitly approved in privacy.approved_owner_equivalents.", ("owner_eq", m.id)))
        else:
            out.append(Leak("CRITICAL", "privacy_admin_bypass", f"Unauthorized Administrator bypass: {m.name}",
                            "Administrator bypasses every overwrite: sees every private floor and the owner logs. "
                            "Remove Administrator from their role(s), or approve the account explicitly.",
                            ("privacy_admin", m.id)))
    # 4) voice powers leak presence via the API (success vs 'not connected' reveals who is in voice)
    if p.voice_powers_forbidden:
        for r in guild.roles.values():
            if r.managed or r.id == guild.id:
                continue
            powers = [x for x in VOICE_POWERS if r.permissions & F.FLAGS[x]]
            holders = [m for m in guild.members_with_role(r.id) if not m.bot and m.id != owner]
            holders = [m for m in holders if not owner_like(m.id)]
            if powers and holders:
                out.append(Leak("WARNING", "privacy_voice_powers", f"@{r.name} has {', '.join(powers)}",
                                "Anyone with these can act on members in rooms they cannot see; the API result reveals "
                                "whether a person is in voice. Use the bot's scoped /voice move instead.",
                                ("privacy_voice_powers", r.id)))
    # 5) member overwrites on private floors that are not active bot-managed passes
    for ch in guild.channels.values():
        at = area_tier(guild, p, ch)
        if at is None or at == pub:
            continue
        for tid, ow in ch.overwrites.items():
            if ow.type == "member" and not owner_like(tid) and tid != bot_id and ow.allow & F.FLAGS["view_channel"] \
                    and (ch.id, tid) not in allowed_member_overwrites:
                name = guild.members[tid].name if tid in guild.members else str(tid)
                out.append(Leak("WARNING", "privacy_member_pass", f"{ch.mention}: standing member pass for {name}",
                                "Private floors should be role-based; temporary passes are removed automatically.",
                                ("privacy_member_pass", ch.id, tid)))
    # 6) unclassified channels (not part of the model)
    for ch in guild.channels.values():
        if area_tier(guild, p, ch) is None:
            out.append(Leak("INFO", "privacy_unclassified", f"{ch.mention} has no privacy tier", "Add it to privacy.areas.",
                            ("privacy_unclassified", ch.id)))
    # 6b) capability monotonicity: T1 >= T2 >= T3 for every social capability in every channel (no stacked roles)
    tmap = {f"tier{i}": int(p.tiers[f"trust_level_{i}"].roles[0]) for i in (1, 2, 3)
            if f"trust_level_{i}" in p.tiers and p.tiers[f"trust_level_{i}"].roles and int(p.tiers[f"trust_level_{i}"].roles[0]) in guild.roles}
    if len(tmap) == 3:
        from ..trust import capability_matrix
        seen_caps = set()
        for v in capability_matrix(guild, tmap)["violations"]:
            k = (v["channel"], v["higher"], v["lower"])
            if k in seen_caps:
                continue
            seen_caps.add(k)
            caps = sorted({x["cap"] for x in capability_matrix(guild, tmap, {v["channel"]})["violations"]
                           if x["higher"] == v["higher"] and x["lower"] == v["lower"]})
            out.append(Leak("WARNING", "tier_capability_inverted",
                            f"{v['lower'].title()} can do more than {v['higher'].title()} in #{v['name']}",
                            f"Missing for the higher tier: {', '.join(caps)}", ("cap_inv", v["channel"], v["higher"], v["lower"])))
    # 7) social privacy: tier roles must not reveal rank to members
    if getattr(p, "social_privacy", True):
        out.extend(social_findings(guild, p))
    # dedupe (member + synthetic can describe the same issue)
    seen, uniq = set(), []
    for x in out:
        if x.key not in seen:
            seen.add(x.key)
            uniq.append(x)
    return uniq


RANK_WORDS = re.compile(r"trust_level|tier|level|rank|vip|elite|core|inner|premium|gold|silver|bronze|platinum|diamond|"
                        r"first|second|third|top|[0-9]|⭐|👑|💎|🥇|🥈|🥉", re.I)


def social_findings(guild: Guild, p) -> list[Leak]:
    """Tier roles are internal; members must not be able to read a ranking from them."""
    out: list[Leak] = []
    roles = [guild.roles[int(r)] for t in p.tiers.values() for r in t.roles if int(r) in guild.roles]
    for r in roles:
        if RANK_WORDS.search(r.name):
            out.append(Leak("WARNING", "social_rank_name", f"Role name '{r.name}' can reveal a ranking",
                            "Use a neutral codename (no numbers, medals, stars, tier words).", ("social_name", r.id)))
        if r.hoist:
            out.append(Leak("WARNING", "social_hoist", f"Role '{r.name}' is shown as its own member-list group",
                            "Turn off 'Display role members separately'.", ("social_hoist", r.id)))
        if r.mentionable:
            out.append(Leak("WARNING", "social_mentionable", f"Role '{r.name}' is mentionable by members",
                            "Turn off 'Allow anyone to @mention this role'.", ("social_mention", r.id)))
    colors = {r.color for r in roles}
    if len(colors) > 1:
        # distinct colours are fine for recognition IF none reads as a medal and all have the same visual weight
        from ..trust import is_prestige_color, rel_luminance
        lum = [rel_luminance(c) for c in colors if c]
        if 0 in colors or any(is_prestige_color(c) for c in colors) or (lum and max(lum) - min(lum) > 0.08):
            out.append(Leak("WARNING", "social_colors", "Tier role colours suggest a ranking",
                            "Use equally bright, non-medal colours for all tier roles (or the default colour for all).",
                            ("social_colors",)))
    return out


def visible_channel_ids(guild: Guild, viewer_id: int) -> set[int]:
    """Channels the viewer can see — the ONLY channels a non-owner tool may read data about."""
    m = guild.members.get(viewer_id)
    if m is None:
        return set()
    return {cid for cid, c in guild.channels.items() if compute(guild, m, c).value & F.FLAGS["view_channel"]}


PUBLIC_EVENT_CATEGORIES = {"voice", "membership"}


def filter_events_for_viewer(rows: list[dict], visible: set[int], viewer_id: int) -> list[dict]:
    """Non-owner data access: only the viewer's own voice/membership history, and only in rooms they can see.
    Security, Guardian, structure, moderation and other people's activity are never returned."""
    out = []
    for e in rows:
        if e.get("category") not in PUBLIC_EVENT_CATEGORIES or e.get("target_id") != viewer_id:
            continue
        if e.get("channel_id") and e["channel_id"] not in visible:
            continue
        out.append(e)
    return out


def redact_for_staff(rows: list[dict], visible: set[int]) -> list[dict]:
    """Non-owner staff views: no events located in rooms they cannot see (Owner area, higher floors, owner logs),
    and no security/bot-internal events. The owner (and approved owner-equivalents) get everything."""
    return [e for e in rows if e.get("category") not in ("security", "bot")
            and (not e.get("channel_id") or e["channel_id"] in visible)]


def visibility_matrix(guild: Guild, cfg) -> dict:
    """{tier: {area_tier: [visible channel names]}} for synthetic members of each tier (owner excluded)."""
    p = cfg.privacy
    res: dict = {}
    for key, t in sorted(p.tiers.items(), key=lambda kv: kv[1].rank):
        if not t.roles and key != p.public_key:
            continue
        sm = Member(-3, key, [int(r) for r in t.roles])
        row: dict = {}
        for ch in guild.channels.values():
            at = area_tier(guild, p, ch)
            if at and ch.type != "category" and compute(guild, sm, ch).value & F.FLAGS["view_channel"]:
                row.setdefault(at, []).append(ch.name)
        res[key] = row
    return res
