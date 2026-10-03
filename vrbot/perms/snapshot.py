"""Convert live discord.py objects into the pure model, and diff two snapshots."""
from __future__ import annotations

from . import flags as F
from .model import Channel, Guild, Member, Overwrite, Role

_TYPE = {0: "text", 2: "voice", 4: "category", 5: "news", 13: "stage", 15: "forum", 16: "media"}


def from_discord(guild, include_members: bool = True) -> Guild:  # guild: discord.Guild
    import discord

    g = Guild(id=guild.id, name=guild.name, owner_id=guild.owner_id, settings={
        "verification_level": str(guild.verification_level),
        "explicit_content_filter": str(guild.explicit_content_filter),
        "default_notifications": str(guild.default_notifications),
        "afk_channel_id": guild.afk_channel.id if guild.afk_channel else None,
        "afk_timeout": guild.afk_timeout,
        "system_channel_id": guild.system_channel.id if guild.system_channel else None,
        "rules_channel_id": guild.rules_channel.id if guild.rules_channel else None,
        "mfa_level": str(guild.mfa_level),
        "member_count": guild.member_count,
    })
    for r in guild.roles:
        g.roles[r.id] = Role(r.id, r.name, r.permissions.value, r.position, r.managed, r.color.value,
                             r.hoist, r.mentionable, len(r.members))
    for c in guild.channels:
        ows = {}
        for target, ow in c.overwrites.items():
            allow, deny = ow.pair()
            ttype = "role" if isinstance(target, discord.Role) else "member"
            ows[target.id] = Overwrite(target.id, ttype, allow.value, deny.value)
        g.channels[c.id] = Channel(
            id=c.id, name=c.name, type=_TYPE.get(c.type.value, str(c.type)),
            parent_id=c.category_id, position=c.position, overwrites=ows,
            topic=getattr(c, "topic", None), nsfw=bool(getattr(c, "nsfw", False)),
            slowmode=getattr(c, "slowmode_delay", 0) or 0,
            bitrate=getattr(c, "bitrate", None), user_limit=getattr(c, "user_limit", None),
        )
    if include_members:
        for m in guild.members:
            g.members[m.id] = member_from_discord(m)
    return g


def member_from_discord(m) -> Member:
    return Member(m.id, m.display_name, [r.id for r in m.roles if r.id != m.guild.id], m.bot, bool(m.is_timed_out()))


def structural_diff(old: Guild, new: Guild) -> list[str]:
    """Human-readable differences between two snapshots (roles, channels, overwrites, settings)."""
    out: list[str] = []
    for k in sorted(set(old.settings) | set(new.settings)):
        if k in ("member_count", "temporary_passes"):
            continue
        if old.settings.get(k) != new.settings.get(k):
            out.append(f"⚙️ setting `{k}`: {old.settings.get(k)} → {new.settings.get(k)}")
    for rid in old.roles.keys() - new.roles.keys():
        out.append(f"➖ role @{old.roles[rid].name} deleted")
    for rid in new.roles.keys() - old.roles.keys():
        out.append(f"➕ role @{new.roles[rid].name} created")
    for rid in old.roles.keys() & new.roles.keys():
        a, b = old.roles[rid], new.roles[rid]
        if a.name != b.name:
            out.append(f"✏️ role @{a.name} renamed to @{b.name}")
        if a.permissions != b.permissions:
            add, rem = b.permissions & ~a.permissions, a.permissions & ~b.permissions
            s = []
            if add:
                s.append("+" + ", ".join(F.names_of(add)))
            if rem:
                s.append("−" + ", ".join(F.names_of(rem)))
            out.append(f"🔑 role @{b.name} permissions: {' '.join(s)}")
        if a.position != b.position:
            out.append(f"↕️ role @{b.name} position {a.position} → {b.position}")
        if a.color != b.color or a.hoist != b.hoist or a.mentionable != b.mentionable:
            out.append(f"🎨 role @{b.name} display settings changed")
    for cid in old.channels.keys() - new.channels.keys():
        out.append(f"➖ channel {old.channels[cid].mention} deleted")
    for cid in new.channels.keys() - old.channels.keys():
        out.append(f"➕ channel {new.channels[cid].mention} created")
    for cid in old.channels.keys() & new.channels.keys():
        a, b = old.channels[cid], new.channels[cid]
        if a.name != b.name:
            out.append(f"✏️ #{a.name} renamed to #{b.name}")
        if a.parent_id != b.parent_id:
            out.append(f"📁 {b.mention} moved category")
        for attr in ("topic", "nsfw", "slowmode", "bitrate", "user_limit"):
            if getattr(a, attr) != getattr(b, attr):
                out.append(f"⚙️ {b.mention} {attr}: {getattr(a, attr)} → {getattr(b, attr)}")
        for tid in a.overwrites.keys() | b.overwrites.keys():
            oa, ob = a.overwrites.get(tid), b.overwrites.get(tid)
            if (oa and (oa.allow, oa.deny)) == (ob and (ob.allow, ob.deny)):
                continue
            who = _target_name(new if tid in new.roles or tid in new.members else old, tid, (oa or ob).type)
            if oa is None:
                out.append(f"🔒 {b.mention}: overwrite added for {who} (allow {F.names_of(ob.allow)}, deny {F.names_of(ob.deny)})")
            elif ob is None:
                out.append(f"🔓 {b.mention}: overwrite removed for {who}")
            else:
                out.append(f"🔒 {b.mention}: overwrite for {who}: allow {F.names_of(oa.allow)}→{F.names_of(ob.allow)}, "
                           f"deny {F.names_of(oa.deny)}→{F.names_of(ob.deny)}")
    return out


def _target_name(g: Guild, tid: int, ttype: str) -> str:
    if tid == g.id:
        return "@everyone"
    if ttype == "role":
        return "@" + g.roles[tid].name if tid in g.roles else f"role {tid}"
    return g.members[tid].name if tid in g.members else f"member {tid}"


def restore_changes(current: Guild, backup: Guild):
    """Permission-level restore plan: role permissions and channel overwrites for objects
    that still exist. Returns (changes, not_restorable_notes)."""
    from .audit import diff_changes

    target = current.clone()
    notes: list[str] = []
    for rid, r in backup.roles.items():
        if rid in target.roles:
            target.roles[rid].permissions = r.permissions
        else:
            notes.append(f"Role @{r.name} no longer exists (recreate manually; IDs cannot be restored).")
    for cid, c in backup.channels.items():
        if cid not in target.channels:
            notes.append(f"Channel {c.mention} no longer exists (recreate manually; messages cannot be restored).")
            continue
        ows = {}
        for tid, ow in c.overwrites.items():
            if (ow.type == "role" and tid not in target.roles) or (ow.type == "member" and tid not in target.members):
                continue
            ows[tid] = Overwrite(tid, ow.type, ow.allow, ow.deny)
        target.channels[cid].overwrites = ows
    return diff_changes(current, target), notes
