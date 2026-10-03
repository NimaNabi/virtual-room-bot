"""Permission audit ("Server Doctor"), Level drift detection and safe repair planning.

Everything here is pure: it works on a model.Guild and returns findings / plans.
Repairs are expressed as small ops, simulated on a clone of the guild, and the
simulated result is re-audited so a plan that would create new problems is flagged
before anything touches Discord.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..config import BaselineCfg, ChannelRule, ServerConfig
from . import flags as F
from .engine import apply_overwrites, base_permissions, compute
from .model import Channel, Guild, Member, Overwrite

CRITICAL, WARNING, INFO = "CRITICAL", "WARNING", "INFO"
SEV_ORDER = {CRITICAL: 0, WARNING: 1, INFO: 2}

# Permissions the bot needs for its modules (see docs/DISCORD_SETUP.md for reasons).
BOT_REQUIRED = [
    "view_channel", "send_messages", "embed_links", "attach_files", "read_message_history",
    "view_audit_log", "manage_roles", "manage_channels", "kick_members", "ban_members",
    "moderate_members", "manage_messages", "connect", "speak",
]


@dataclass
class Op:
    """A single intended mutation. kind: role_add|role_remove|ow_allow|ow_deny|ow_neutral"""
    kind: str
    bits: int
    role_id: int | None = None
    channel_id: int | None = None
    target_id: int | None = None
    target_type: str = "role"


@dataclass
class Finding:
    severity: str
    code: str
    title: str
    detail: str = ""
    key: tuple = ()
    ops: list[Op] = field(default_factory=list)
    trust_level: str | None = None

    @property
    def fixable(self) -> bool:
        return bool(self.ops)


# ------------------------------------------------------------------ helpers
def resolve_role(guild: Guild, ref: str):
    if ref.isdigit() and int(ref) in guild.roles:
        return guild.roles[int(ref)]
    if ref.lower() in ("@everyone", "everyone"):
        return guild.everyone
    return guild.role_by_name(ref)


def match_channels(guild: Guild, rule: ChannelRule) -> list[Channel]:
    m = rule.match.strip()
    if m.startswith("id:"):
        c = guild.channels.get(int(m[3:]))
        if c is None:
            return []
        # an ID of a category covers the category and every channel in it (like "category:Name")
        return [c, *guild.children(c.id)] if c.type == "category" else [c]
    if m.lower().startswith("category:"):
        name = m.split(":", 1)[1].strip().lower()
        cats = [c for c in guild.channels.values() if c.type == "category" and c.name.lower() == name]
        out: list[Channel] = []
        for cat in cats:
            out.append(cat)
            out.extend(guild.children(cat.id))
        return out
    if m == "*":
        return list(guild.channels.values())
    return guild.channels_named(m)


def synthetic(role_id: int | None) -> Member:
    return Member(id=-1, name="(synthetic)", role_ids=[role_id] if role_id else [])


def expectations_for(guild: Guild, baseline: BaselineCfg, privacy=None) -> dict[int, dict[str, dict[str, bool]]]:
    """channel_id -> trust_level_key -> {perm: expected}. Privacy tiers are applied first; explicit channel
    rules afterwards (later rules override earlier)."""
    out: dict[int, dict[str, dict[str, bool]]] = {}
    if privacy is not None and privacy.tiers:
        from .privacy import tier_expectations
        for cid, per in tier_expectations(guild, privacy).items():
            for k, exp in per.items():
                if k == "default" or k in baseline.trust_levels:
                    out.setdefault(cid, {}).setdefault(k, {}).update(exp)
    keys = list(baseline.trust_levels) + ["default"]
    for rule in baseline.channels:
        for ch in match_channels(guild, rule):
            for k in keys:
                exp = rule.expectations(k)
                if exp is None:
                    continue
                exp = {p: v for p, v in exp.items() if not (ch.type == "category" and p in ("connect", "speak", "send_messages"))}
                if ch.is_voice:
                    exp = {p: v for p, v in exp.items() if p not in ("send_messages",)}
                elif ch.type != "category":
                    exp = {p: v for p, v in exp.items() if p not in ("connect", "speak")}
                out.setdefault(ch.id, {}).setdefault(k, {}).update(exp)
    return out


def _names(bits: int) -> str:
    return ", ".join(F.pretty(n) for n in F.names_of(bits))


# ------------------------------------------------------------------ audit
def audit(guild: Guild, cfg: ServerConfig, bot_id: int | None = None, owner_ids: list[int] | None = None) -> list[Finding]:
    out: list[Finding] = []
    bl = cfg.baseline
    owners = set(owner_ids or []) | set(cfg.access.owner_ids) | {guild.owner_id}
    admin_role_ids = {r.id for n in cfg.access.admin_roles if (r := resolve_role(guild, n))}
    mod_role_ids = {r.id for n in cfg.access.moderator_roles if (r := resolve_role(guild, n))}
    trust_level_roles: dict[str, object] = {}

    # --- @everyone
    ev = guild.everyone
    bad = ev.permissions & F.value_of(cfg.baseline.default.forbid)
    if bad:
        crit = bad & F.value_of(F.CRITICAL)
        out.append(Finding(CRITICAL if crit else WARNING, "everyone_dangerous",
                           f"@everyone has dangerous permissions: {_names(bad)}",
                           "Every member of the server inherits these.", ("everyone",),
                           [Op("role_remove", bad, role_id=ev.id)], trust_level="default"))

    missing_ev = F.value_of(cfg.baseline.default.require) & ~ev.permissions
    if missing_ev:
        why = []
        if missing_ev & F.FLAGS["read_message_history"]:
            why.append("without Read Message History members only see messages sent while they are looking (e.g. #rules looks empty)")
        if missing_ev & F.FLAGS["use_application_commands"]:
            why.append("without Use Application Commands they cannot use any bot's slash commands (/music, /help)")
        out.append(Finding(WARNING, "everyone_missing_perm", f"@everyone (ordinary members) lacks: {_names(missing_ev)}",
                           "; ".join(why) or "Required by baseline.default.require.", ("everyone_missing",),
                           [Op("role_add", missing_ev, role_id=ev.id)], trust_level="default"))

    # --- Administrator on unexpected roles / members
    for r in guild.roles.values():
        if r.id == guild.id:
            continue
        if r.managed:
            bot_members = [m for m in guild.members_with_role(r.id) if m.bot and m.id != bot_id]
            if r.permissions & F.FLAGS["administrator"] and bot_members:
                out.append(Finding(CRITICAL, "third_party_bot_admin", f"Third-party bot {bot_members[0].name} has Administrator",
                                   "If that bot or its token is ever compromised, it can delete the whole server. "
                                   "Give it only what it needs (e.g. Connect/Speak for music).", ("bot_admin3", r.id)))
            continue
        is_trust_level = any(resolve_role(guild, c.role) is r for c in bl.trust_levels.values())
        if r.id in set(guild.settings.get("elevation_roles", [])):
            continue  # bot-managed temporary authority role (expiring grants; Guardian watches manual use)
        if r.permissions & F.FLAGS["administrator"] and r.id not in admin_role_ids and not is_trust_level:
            # (a Level role with Administrator is reported once as trust_level_extra_perm)
            holders = [m.name for m in guild.members_with_role(r.id) if not m.bot]
            out.append(Finding(CRITICAL, "unexpected_admin_role", f"Role @{r.name} has Administrator",
                               f"Not listed in access.admin_roles. Holders: {', '.join(holders[:10]) or 'none'}", ("admin_role", r.id)))

    # --- Trust levels
    for key, c in sorted(bl.trust_levels.items(), key=lambda kv: kv[1].rank):
        role = resolve_role(guild, c.role)
        if role is None:
            out.append(Finding(WARNING, "trust_level_role_missing", f"Level `{key}` role '{c.role}' does not exist",
                               "Create the role or fix config/server.yaml.", ("trust_level_missing", key), trust_level=key))
            continue
        trust_level_roles[key] = role
        missing = F.value_of(c.require) & ~role.permissions
        if missing:
            out.append(Finding(WARNING, "trust_level_missing_perm", f"@{role.name} ({key}) is missing: {_names(missing)}",
                               "Required by the Level baseline.", ("trust_level_missing_perm", key),
                               [Op("role_add", missing, role_id=role.id)], trust_level=key))
        forbidden = F.value_of(c.forbid) | (F.value_of(F.DANGEROUS) & ~F.value_of(c.allow_dangerous) & ~F.value_of(c.require))
        extra = role.permissions & forbidden
        if extra:
            sev = CRITICAL if extra & F.value_of(F.CRITICAL) else WARNING
            holders = [m.name for m in guild.members_with_role(role.id) if not m.bot]
            out.append(Finding(sev, "trust_level_extra_perm", f"@{role.name} ({key}) unexpectedly has: {_names(extra)}",
                               "Not allowed by the Level baseline (privilege escalation risk)."
                               + (f" Holders ({len(holders)}): {', '.join(holders[:12])}" if sev == CRITICAL else ""),
                               ("trust_level_extra_perm", key), [Op("role_remove", extra, role_id=role.id)], trust_level=key))

    # trust_level ordering: rank 1 must sit above rank 2, etc.
    ranked = sorted(((c.rank, k) for k, c in bl.trust_levels.items() if k in trust_level_roles)) if bl.enforce_trust_level_order else []
    for (ra, ka), (rb, kb) in zip(ranked, ranked[1:]):
        a, b = trust_level_roles[ka], trust_level_roles[kb]
        if a.position <= b.position:
            out.append(Finding(WARNING, "trust_level_order", f"Role order: @{a.name} ({ka}) should be above @{b.name} ({kb})",
                               "Drag it higher in Server Settings → Roles. (Order matters for colors and moderation.)",
                               ("trust_level_order", ka, kb)))

    # members in multiple trust_levels
    trust_level_ids = {r.id: k for k, r in trust_level_roles.items()}
    multi = [m for m in guild.members.values() if sum(1 for rid in m.role_ids if rid in trust_level_ids) > 1]
    if multi:
        out.append(Finding(INFO, "multi_trust_level", f"{len(multi)} member(s) are in more than one Level",
                           ", ".join(m.name for m in multi[:15]), ("multi_trust_level",)))

    # --- explicit hierarchy list
    hier = [r for n in bl.hierarchy if (r := resolve_role(guild, n))]
    for a, b in zip(hier, hier[1:]):
        if a.position <= b.position:
            out.append(Finding(WARNING, "hierarchy_order", f"@{a.name} should be above @{b.name}",
                               "Configured in baseline.hierarchy. Moderators cannot act on members whose top role is equal or higher.",
                               ("hier", a.id, b.id)))

    # --- privileged members beyond their design
    privileged_ok = admin_role_ids | mod_role_ids
    for m in guild.members.values():
        if m.bot or m.id in owners:
            continue
        if any(r in privileged_ok for r in m.role_ids):
            continue
        member_trust_level = next((trust_level_ids[r] for r in m.role_ids if r in trust_level_ids), None)
        allowed = F.value_of(bl.trust_levels[member_trust_level].allow_dangerous) | F.value_of(bl.trust_levels[member_trust_level].require) if member_trust_level else 0
        if base_permissions(guild, m).admin:
            continue  # already reported once per role (unexpected_admin_role lists the holders)
        # powers from non-Level roles only (a Level role's own extras are reported as trust_level_extra_perm)
        perms = 0
        for rid in m.role_ids:
            if rid in guild.roles and rid not in trust_level_ids:
                perms |= guild.roles[rid].permissions
        extra = perms & F.value_of(F.DANGEROUS) & ~allowed & ~ev.permissions
        if extra:
            via = [f"@{guild.roles[r].name}" for r in m.role_ids if r in guild.roles and guild.roles[r].permissions & extra]
            out.append(Finding(WARNING, "member_overprivileged", f"{m.name} has {_names(extra)} beyond their Level",
                               f"Level: {member_trust_level or 'none'}; via {', '.join(via)}", ("member_priv", m.id)))

    # --- bot
    if bot_id and bot_id in guild.members:
        bot = guild.members[bot_id]
        bp = base_permissions(guild, bot)
        missing = F.value_of(BOT_REQUIRED) & ~bp.value
        if missing:
            out.append(Finding(CRITICAL, "bot_missing_perm", f"The bot is missing: {_names(missing)}",
                               "Some modules will not work. Re-invite with the link from docs/DISCORD_SETUP.md or edit the bot role.",
                               ("bot_missing",)))
        if bp.admin and guild.owner_id != bot_id and not cfg.security.bot_admin_intended:
            out.append(Finding(INFO, "bot_is_admin", "The bot has Administrator",
                               "Set security.bot_admin_intended: true if this is deliberate.", ("bot_admin",)))
        top = guild.top_role(bot)
        for key, role in trust_level_roles.items():
            if role.position >= top.position:
                out.append(Finding(CRITICAL, "bot_below_trust_level", f"The bot's role @{top.name} is not above @{role.name} ({key})",
                                   "The bot cannot manage or repair this Level role. Drag the bot role higher.", ("bot_below", role.id)))
        for rid in mod_role_ids | admin_role_ids:
            r = guild.roles[rid]
            if r.position >= top.position and rid in mod_role_ids:
                out.append(Finding(INFO, "bot_below_mod", f"the bot's role is below @{r.name}",
                                   "Fine if intended: the bot cannot moderate members holding that role.", ("bot_below_mod", rid)))
        for ch in guild.channels.values():
            if not compute(guild, bot, ch).value & F.FLAGS["view_channel"]:
                out.append(Finding(WARNING, "bot_cannot_view", f"The bot cannot see {ch.mention}",
                                   "It cannot log, audit live state for, or repair this channel. Add an ALLOW View Channel for the bot role.",
                                   ("bot_view", ch.id)))

    # --- channel overwrite hygiene
    for ch in sorted(guild.channels.values(), key=lambda c: c.position):
        for tid, ow in ch.overwrites.items():
            if ow.type == "role" and tid not in guild.roles:
                out.append(Finding(INFO, "orphan_overwrite", f"{ch.mention} has an overwrite for a deleted role",
                               "Harmless but clutter; remove it.", ("orphan", ch.id, tid),
                               [Op("ow_neutral", F.ALL, channel_id=ch.id, target_id=tid)]))
            elif ow.type == "member":
                if tid not in guild.members:
                    out.append(Finding(INFO, "orphan_overwrite", f"{ch.mention} has an overwrite for a member who left",
                                       "", ("orphan", ch.id, tid), [Op("ow_neutral", F.ALL, channel_id=ch.id, target_id=tid, target_type="member")]))
                elif not bl.allow_member_overwrites and tid != bot_id and tid not in owners:  # bot/owner overwrites are by design
                    who = guild.members[tid].name
                    what = []
                    if ow.allow:
                        what.append(f"allow {_names(ow.allow)}")
                    if ow.deny:
                        what.append(f"deny {_names(ow.deny)}")
                    out.append(Finding(WARNING, "member_overwrite", f"{ch.mention}: member-specific overwrite for {who}",
                                       f"Bypasses the Level design ({'; '.join(what)}).", ("member_ow", ch.id, tid)))
        # conflicting role overwrites for key perms
        key_bits = F.value_of(["view_channel", "send_messages", "connect", "speak"])
        role_ows = [(tid, o) for tid, o in ch.overwrites.items() if o.type == "role" and tid != guild.id and tid in guild.roles]
        for i, (ta, a) in enumerate(role_ows):
            for tb, b in role_ows[i + 1:]:
                c1 = a.allow & b.deny & key_bits
                c2 = b.allow & a.deny & key_bits
                for allow_r, deny_r, bits in ((ta, tb, c1), (tb, ta, c2)):
                    if bits:
                        out.append(Finding(INFO, "conflicting_overwrite",
                                           f"{ch.mention}: @{guild.roles[allow_r].name} ALLOWS but @{guild.roles[deny_r].name} DENIES {_names(bits)}",
                                           "Members with both roles get ALLOW (role allows win).", ("conflict", ch.id, allow_r, deny_r)))
        # unsynced channels
        if ch.parent_id and guild.is_synced(ch) is False and not {ch.name, f"#{ch.name}", str(ch.id)} & set(bl.allow_unsynced):
            out.append(Finding(INFO, "unsynced", f"{ch.mention} differs from its category {guild.channels[ch.parent_id].name}",
                               "Its overwrites are not inherited any more. Add it to baseline.allow_unsynced if intentional.",
                               ("unsynced", ch.id)))
        # unreachable: no non-admin role can view
        if ch.type != "category":
            viewers = [r for r in guild.roles.values() if not r.permissions & F.FLAGS["administrator"] and not r.managed
                       and compute(guild, synthetic(r.id if r.id != guild.id else None), ch).value & F.FLAGS["view_channel"]]
            if not viewers:
                out.append(Finding(INFO, "admin_only_channel", f"{ch.mention} is visible to admins only",
                                   "Fine for staff channels; otherwise nobody else can see it.", ("admin_only", ch.id)))

    # --- expectations per channel/trust_level
    exps = expectations_for(guild, bl, cfg.privacy)
    for ch_id, per_key in exps.items():
        ch = guild.channels[ch_id]
        for key, exp in per_key.items():
            role = trust_level_roles.get(key) if key != "default" else guild.everyone
            if role is None:
                continue
            m = synthetic(role.id if key != "default" else None)
            eff = compute(guild, m, ch).value
            base = base_permissions(guild, m).value
            wrong_yes = [p for p, v in exp.items() if v and not eff & F.FLAGS[p]]
            # root cause dedupe: a required basic missing at server level (and untouched in this channel)
            # is already reported once by everyone_missing_perm
            touched = 0
            for tid in (guild.id, role.id):
                if (o := ch.overwrites.get(tid)):
                    touched |= o.allow | o.deny
            wrong_yes = [p for p in wrong_yes if not (p in cfg.baseline.default.require and not base & F.FLAGS[p]
                                                      and not touched & F.FLAGS[p] and missing_ev & F.FLAGS[p])]
            wrong_no = [p for p, v in exp.items() if not v and eff & F.FLAGS[p]]
            if not wrong_yes and not wrong_no:
                continue
            label = f"@{role.name}" if key != "default" else "@everyone (default)"
            parts = []
            if wrong_yes:
                parts.append(f"cannot {', '.join(F.pretty(p) for p in wrong_yes)}")
            if wrong_no:
                parts.append(f"can {', '.join(F.pretty(p) for p in wrong_no)} but should not")
            # a channel visible to a trust_level that should not see it = private channel exposed
            sev = CRITICAL if "view_channel" in wrong_no else WARNING
            out.append(Finding(sev, "channel_access", f"{ch.mention}: {label} {' and '.join(parts)}",
                               "Differs from the Level channel baseline.", ("chan", ch.id, key),
                               _channel_fix_ops(guild, ch, role.id, key == "default", exp), trust_level=key))

    # privacy floors: issues not already expressed as channel_access findings
    if cfg.privacy.tiers:
        from .privacy import privacy_audit
        passes = {tuple(x) for x in guild.settings.get("temporary_passes", [])}
        for lk in privacy_audit(guild, cfg, bot_id, passes):
            if lk.code in ("privacy_leak", "privacy_admin_bypass", "privacy_voice_powers", "privacy_member_pass",
                           "privacy_owner_equivalent", "social_rank_name", "social_hoist", "social_mentionable", "social_colors"):
                out.append(Finding(lk.severity, lk.code, lk.title, lk.detail, lk.key))
    out.sort(key=lambda f: (SEV_ORDER[f.severity], f.code, f.title))
    return out


def _channel_fix_ops(guild: Guild, ch: Channel, role_id: int, is_default: bool, exp: dict[str, bool]) -> list[Op]:
    """Minimal overwrite ops so the synthetic member for role_id meets `exp` in ch."""
    sim = guild.clone()
    ops: list[Op] = []
    target = guild.id if is_default else role_id
    member = synthetic(None if is_default else role_id)
    # view first: other perms depend on it
    order = sorted(exp, key=lambda p: 0 if p == "view_channel" else 1)
    for p in order:
        want, bit = exp[p], F.FLAGS[p]
        c = sim.channels[ch.id]
        explicit = apply_overwrites(sim, member, c, base_permissions(sim, member).value)
        if bool(explicit & bit) == want:
            continue
        ow = c.overwrites.get(target)
        if want:
            if ow and ow.deny & bit:
                op = Op("ow_neutral", bit, channel_id=ch.id, target_id=target)
                apply_op(sim, op)
                ex2 = apply_overwrites(sim, member, sim.channels[ch.id], base_permissions(sim, member).value)
                if ex2 & bit:
                    ops.append(op)
                    continue
            # ow_allow also clears any deny for this bit, so no undo of the neutral attempt is needed
            op = Op("ow_allow", bit, channel_id=ch.id, target_id=target)
        else:
            if ow and ow.allow & bit:
                op = Op("ow_neutral", bit, channel_id=ch.id, target_id=target)
                apply_op(sim, op)
                ex2 = apply_overwrites(sim, member, sim.channels[ch.id], base_permissions(sim, member).value)
                if not ex2 & bit:
                    ops.append(op)
                    continue
            op = Op("ow_deny", bit, channel_id=ch.id, target_id=target)
        apply_op(sim, op)
        ops.append(op)
    return ops


# ------------------------------------------------------------------ ops & plans
def apply_op(g: Guild, op: Op) -> None:
    if op.kind in ("role_add", "role_remove"):
        r = g.roles[op.role_id]
        r.permissions = (r.permissions | op.bits) if op.kind == "role_add" else (r.permissions & ~op.bits)
        return
    ch = g.channels[op.channel_id]
    ow = ch.overwrites.get(op.target_id) or Overwrite(op.target_id, op.target_type)
    if op.kind == "ow_allow":
        ow.allow |= op.bits
        ow.deny &= ~op.bits
    elif op.kind == "ow_deny":
        ow.deny |= op.bits
        ow.allow &= ~op.bits
    elif op.kind == "ow_neutral":
        ow.allow &= ~op.bits
        ow.deny &= ~op.bits
    if ow.allow == 0 and ow.deny == 0:
        ch.overwrites.pop(op.target_id, None)
    else:
        ch.overwrites[op.target_id] = ow


@dataclass
class Change:
    kind: str  # role_perms | overwrite
    target_id: int
    target_name: str
    channel_id: int | None = None
    channel_name: str | None = None
    target_type: str = "role"
    before: object = None  # int for role_perms; [allow, deny] or None for overwrite
    after: object = None

    def describe(self) -> str:
        if self.kind == "role_perms":
            add, rem = self.after & ~self.before, self.before & ~self.after
            parts = []
            if add:
                parts.append(f"+{_names(add)}")
            if rem:
                parts.append(f"−{_names(rem)}")
            return f"Role @{self.target_name}: {'; '.join(parts)}"
        b = self.before or [0, 0]
        a = self.after or [0, 0]
        parts = []
        for label, idx in (("ALLOW", 0), ("DENY", 1)):
            add, rem = a[idx] & ~b[idx], b[idx] & ~a[idx]
            if add:
                parts.append(f"{label} +{_names(add)}")
            if rem:
                parts.append(f"{label} −{_names(rem)}")
        if self.after is None:
            parts.append("(overwrite removed)")
        elif self.before is None:
            parts.append("(new overwrite)")
        return f"#{self.channel_name} → {self.target_name}: {'; '.join(parts)}"

    def to_dict(self) -> dict:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, d: dict) -> "Change":
        return cls(**d)


def diff_changes(before: Guild, after: Guild) -> list[Change]:
    changes: list[Change] = []
    for rid, r in after.roles.items():
        old = before.roles.get(rid)
        if old and old.permissions != r.permissions:
            changes.append(Change("role_perms", rid, r.name if rid != after.id else "everyone", before=old.permissions, after=r.permissions))
    for cid, ch in after.channels.items():
        old = before.channels.get(cid)
        if not old:
            continue
        for tid in set(ch.overwrites) | set(old.overwrites):
            a, b = ch.overwrites.get(tid), old.overwrites.get(tid)
            av = [a.allow, a.deny] if a else None
            bv = [b.allow, b.deny] if b else None
            if av != bv:
                ttype = (a or b).type
                name = ("@everyone" if tid == after.id else "@" + after.roles[tid].name) if ttype == "role" and tid in after.roles \
                    else (after.members[tid].name if tid in after.members else str(tid))
                changes.append(Change("overwrite", tid, name, cid, ch.name, ttype, bv, av))
    return changes


@dataclass
class Plan:
    changes: list[Change]
    resolved: list[Finding]
    introduced: list[Finding]
    blocked: list[str]
    remaining: list[Finding]

    @property
    def safe(self) -> bool:
        return not self.introduced and not self.blocked


def plan_repair(guild: Guild, cfg: ServerConfig, bot_id: int | None, scope: str | None = None,
                codes: set[str] | None = None) -> Plan:
    """scope: trust_level key or 'default' to limit; codes: finding codes to include."""
    before = audit(guild, cfg, bot_id)
    sim = guild.clone()
    chosen = [f for f in before if f.ops and (scope is None or f.trust_level == scope) and (codes is None or f.code in codes)]
    for f in chosen:
        for op in f.ops:
            if op.kind.startswith("ow_") and op.channel_id not in sim.channels:
                continue
            apply_op(sim, op)
    changes = diff_changes(guild, sim)
    after = audit(sim, cfg, bot_id)
    bk = {f.key for f in before}
    ak = {f.key for f in after}
    resolved = [f for f in before if f.key not in ak]
    introduced = [f for f in after if f.key not in bk]
    blocked = check_bot_can_apply(guild, bot_id, changes)
    return Plan(changes, resolved, introduced, blocked, after)


def check_bot_can_apply(guild: Guild, bot_id: int | None, changes: list[Change]) -> list[str]:
    if not bot_id or bot_id not in guild.members:
        return ["Bot member not found in snapshot."] if changes else []
    bot = guild.members[bot_id]
    bp = base_permissions(guild, bot)
    top = guild.top_role(bot)
    out = []
    for c in changes:
        if c.kind == "role_perms":
            r = guild.roles[c.target_id]
            if r.managed:
                out.append(f"@{r.name} is managed by an integration and cannot be edited.")
            if not bp.admin:
                if r.position >= top.position and r.id != guild.id:
                    out.append(f"@{r.name} is not below the bot's top role @{top.name}.")
                if not bp.value & F.FLAGS["manage_roles"]:
                    out.append("Bot lacks Manage Roles.")
                grant = c.after & ~c.before & ~bp.value
                if grant:
                    out.append(f"Bot cannot grant permissions it does not have: {_names(grant)}.")
        else:
            if bp.admin:
                continue
            ch = guild.channels[c.channel_id]
            cp = compute(guild, bot, ch).value
            if not cp & F.FLAGS["manage_roles"] or not cp & F.FLAGS["view_channel"]:
                out.append(f"Bot needs View Channel + Manage Permissions in #{ch.name}.")
            touched = 0
            for i in (0, 1):
                a = (c.after or [0, 0])[i]
                b = (c.before or [0, 0])[i]
                touched |= a ^ b
            lacking = touched & ~cp & ~F.FLAGS["manage_roles"]
            if lacking:
                out.append(f"#{ch.name}: bot can only set permissions it has itself; lacks {_names(lacking)}.")
    return sorted(set(out))
