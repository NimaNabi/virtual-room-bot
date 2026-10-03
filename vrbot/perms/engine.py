"""Discord permission computation + human explanation ("Permission Doctor").

Implements the official algorithm (docs.discord.com/developers/topics/permissions):
  base = @everyone | all member roles   (owner/ADMINISTRATOR => ALL)
  channel: @everyone overwrite deny, then allow
           role overwrites: union(deny) removed, then union(allow) added
           member overwrite deny, then allow
  implicit: no VIEW_CHANNEL => nothing in channel; no SEND_MESSAGES => no embeds/files/tts/@everyone;
            no CONNECT (voice) => no speak/stream/...; timed-out => only VIEW + READ_HISTORY.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import flags as F
from .model import Channel, Guild, Member


@dataclass
class Computed:
    value: int
    owner: bool = False
    admin: bool = False


def base_permissions(guild: Guild, member: Member) -> Computed:
    if member.id == guild.owner_id:
        return Computed(F.ALL, owner=True, admin=True)
    perms = guild.everyone.permissions
    for rid in member.role_ids:
        role = guild.roles.get(rid)
        if role:
            perms |= role.permissions
    if perms & F.FLAGS["administrator"]:
        return Computed(F.ALL, admin=True)
    return Computed(perms)


def apply_overwrites(guild: Guild, member: Member, channel: Channel, base: int) -> int:
    perms = base
    ev = channel.overwrites.get(guild.id)
    if ev:
        perms &= ~ev.deny
        perms |= ev.allow
    allow = deny = 0
    for rid in member.role_ids:
        ow = channel.overwrites.get(rid)
        if ow and ow.type == "role":
            allow |= ow.allow
            deny |= ow.deny
    perms &= ~deny
    perms |= allow
    mo = channel.overwrites.get(member.id)
    if mo and mo.type == "member":
        perms &= ~mo.deny
        perms |= mo.allow
    return perms


def apply_implicit(perms: int, channel: Channel | None, timed_out: bool, admin: bool) -> int:
    if admin:
        return perms
    if timed_out:
        perms &= F.value_of(F.TIMEOUT_ALLOWED)
    if channel is None:
        return perms
    if not perms & F.FLAGS["view_channel"]:
        return 0
    if channel.type != "category" and not channel.is_voice and not perms & F.FLAGS["send_messages"]:
        perms &= ~F.value_of(F.TEXT_SEND_DEPENDENT)
    if channel.is_voice and not perms & F.FLAGS["connect"]:
        perms &= ~F.value_of(F.VOICE_CONNECT_DEPENDENT)
    return perms


def compute(guild: Guild, member: Member, channel: Channel | None = None) -> Computed:
    base = base_permissions(guild, member)
    if base.admin:
        return base
    value = base.value if channel is None else apply_overwrites(guild, member, channel, base.value)
    return Computed(apply_implicit(value, channel, member.timed_out, False))


# ---------------------------------------------------------------------------
# Explanations
# ---------------------------------------------------------------------------

@dataclass
class PermTrace:
    perm: str
    allowed: bool
    steps: list[str] = field(default_factory=list)
    reason: str = ""
    fix: str | None = None


@dataclass
class Explanation:
    member: Member
    channel: Channel | None
    traces: list[PermTrace]
    notes: list[str] = field(default_factory=list)

    def render(self, guild: Guild) -> str:
        where = self.channel.mention if self.channel else "(server-wide)"
        lines = [f"**User:** {self.member.name}", f"**Channel:** {where}", ""]
        for t in self.traces:
            lines.append(f"{'✅' if t.allowed else '❌'} **{F.pretty(t.perm)}: {'YES' if t.allowed else 'NO'}**")
        for t in self.traces:
            lines.append("")
            lines.append(f"__{F.pretty(t.perm)}__ — {t.reason}")
            for s in t.steps:
                lines.append(f"  • {s}")
            if t.fix:
                lines.append(f"  🔧 **Recommended fix:** {t.fix}")
        if self.notes:
            lines.append("")
            lines.extend(f"ℹ️ {n}" for n in self.notes)
        return "\n".join(lines)


def _rname(guild: Guild, rid: int) -> str:
    if rid == guild.id:
        return "@everyone"
    r = guild.roles.get(rid)
    return f"@{r.name}" if r else f"<deleted role {rid}>"


def _trace_one(guild: Guild, member: Member, channel: Channel | None, perm: str) -> PermTrace:
    bit = F.FLAGS[perm]
    t = PermTrace(perm, False)
    pp = F.pretty(perm)
    if member.id == guild.owner_id:
        t.allowed, t.reason = True, "Server owner always has every permission."
        return t
    member_roles = [guild.roles[r] for r in member.role_ids if r in guild.roles]
    admin_roles = [r for r in member_roles + [guild.everyone] if r.permissions & F.FLAGS["administrator"]]
    if admin_roles:
        t.allowed = True
        t.reason = f"Administrator via {', '.join('@' + r.name for r in admin_roles)} bypasses all overwrites."
        return t

    granting = [r for r in [guild.everyone] + member_roles if r.permissions & bit]
    state = bool(granting)
    if granting:
        t.steps.append(f"Server level: granted by {', '.join(_rname(guild, r.id) for r in granting)}.")
        decisive = ("base_allow", granting)
    else:
        t.steps.append("Server level: none of the member's roles (or @everyone) grant it.")
        decisive = ("base_missing", None)

    if channel is not None and perm not in F.GUILD_ONLY:
        ev = channel.overwrites.get(guild.id)
        if ev and ev.deny & bit:
            state = False
            t.steps.append(f"{channel.mention}: @everyone overwrite DENIES it.")
            decisive = ("everyone_deny", None)
        if ev and ev.allow & bit:
            state = True
            t.steps.append(f"{channel.mention}: @everyone overwrite ALLOWS it.")
            decisive = ("everyone_allow", None)
        allow_r = [rid for rid in member.role_ids if (o := channel.overwrites.get(rid)) and o.type == "role" and o.allow & bit]
        deny_r = [rid for rid in member.role_ids if (o := channel.overwrites.get(rid)) and o.type == "role" and o.deny & bit]
        if deny_r:
            t.steps.append(f"{channel.mention}: role overwrite DENIES it for {', '.join(_rname(guild, r) for r in deny_r)}.")
            if not allow_r:
                state = False
                decisive = ("role_deny", deny_r)
        if allow_r:
            state = True
            extra = " (role ALLOW beats role DENY)" if deny_r else ""
            t.steps.append(f"{channel.mention}: role overwrite ALLOWS it for {', '.join(_rname(guild, r) for r in allow_r)}{extra}.")
            decisive = ("role_allow", allow_r)
        mo = channel.overwrites.get(member.id)
        if mo and mo.type == "member":
            if mo.deny & bit:
                state = False
                t.steps.append(f"{channel.mention}: member-specific overwrite DENIES it.")
                decisive = ("member_deny", None)
            if mo.allow & bit:
                state = True
                t.steps.append(f"{channel.mention}: member-specific overwrite ALLOWS it.")
                decisive = ("member_allow", None)

    t.allowed = state
    kind, data = decisive
    ch = channel.mention if channel else "the server"
    member_role_names = ", ".join("@" + r.name for r in sorted(member_roles, key=lambda r: -r.position)) or "only @everyone"

    if state:
        if kind == "base_allow":
            t.reason = f"Granted at server level by {', '.join(_rname(guild, r.id) for r in data)} and not denied in {ch}."
        elif kind == "everyone_allow":
            t.reason = f"{ch} explicitly allows it for @everyone."
        elif kind == "role_allow":
            t.reason = f"{ch} explicitly allows it for {', '.join(_rname(guild, r) for r in data)}."
        elif kind == "member_allow":
            t.reason = f"{ch} has a member-specific ALLOW."
        else:
            t.reason = "Allowed."
    else:
        if kind == "base_missing":
            t.reason = f"No role the member has grants {pp} at server level, and nothing in {ch} allows it."
            t.fix = (f"Add an ALLOW for {pp} on {ch} for one of the member's roles ({member_role_names}), "
                     f"or grant {pp} to that role at server level.")
        elif kind == "everyone_deny":
            t.reason = f"{ch} DENIES {pp} for @everyone and none of the member's roles has an ALLOW overwrite there."
            t.fix = f"Add an ALLOW for {pp} on {ch} for the member's role ({member_role_names})."
        elif kind == "role_deny":
            names = ", ".join(_rname(guild, r) for r in data)
            t.reason = f"{ch} has an explicit DENY for {pp} on {names}" + (
                f", which overrides the server-level grant from {', '.join(_rname(guild, r.id) for r in granting)}." if granting else ".")
            t.fix = f"Remove the DENY for {pp} from the {names} overwrite on {ch} (or add an ALLOW on another role the member has)."
        elif kind == "member_deny":
            t.reason = f"{ch} has a member-specific DENY for {pp} (this bypasses the normal role design)."
            t.fix = f"Remove the member-specific overwrite for {member.name} on {ch}."

    # implicit rules, applied after explicit calculation (guild-only perms ignore channel context)
    if channel is not None and perm in F.GUILD_ONLY:
        channel = None
    if channel is not None and state:
        full = compute(guild, member, channel).value
        if not full & bit:
            t.allowed = False
            if member.timed_out and perm not in F.TIMEOUT_ALLOWED:
                t.reason = "Member is currently timed out: only View Channel and Read Message History remain."
                t.fix = "Remove the timeout (/mod untimeout) if it was not intended."
            elif not full & F.FLAGS["view_channel"] and perm != "view_channel":
                t.reason = f"Explicitly allowed, but the member cannot View {ch}, which implicitly denies everything else."
                t.fix = "Fix View Channel first (see below)."
            elif perm in F.TEXT_SEND_DEPENDENT:
                t.reason = f"Allowed, but Send Messages is NO in {ch}, which implicitly denies {pp}."
                t.fix = "Fix Send Messages first."
            elif perm in F.VOICE_CONNECT_DEPENDENT and channel.is_voice:
                t.reason = f"Allowed, but Connect is NO in {ch}, which implicitly denies {pp}."
                t.fix = "Fix Connect first."
    elif channel is None and member.timed_out and state and perm not in F.TIMEOUT_ALLOWED:
        t.allowed = False
        t.reason = "Member is currently timed out."
        t.fix = "Remove the timeout (/mod untimeout) if it was not intended."
    return t


def default_perms_for(channel: Channel | None) -> list[str]:
    if channel is None:
        return ["view_channel", "send_messages", "connect", "speak"]
    if channel.is_voice:
        return ["view_channel", "connect", "speak", "stream"]
    if channel.type == "category":
        return ["view_channel", "send_messages", "connect"]
    return ["view_channel", "send_messages", "read_message_history", "attach_files", "embed_links"]


def explain(guild: Guild, member: Member, channel: Channel | None, perms: list[str] | None = None) -> Explanation:
    perms = [F.normalize(p) for p in (perms or default_perms_for(channel))]
    traces = [_trace_one(guild, member, channel, p) for p in perms]
    notes: list[str] = []
    if channel is not None and channel.parent_id in guild.channels:
        synced = guild.is_synced(channel)
        cat = guild.channels[channel.parent_id]
        if synced is False:
            notes.append(f"{channel.mention} is NOT synced with {cat.mention}; its overwrites differ from the category.")
    if member.timed_out:
        notes.append("Member is timed out.")
    return Explanation(member, channel, traces, notes)
