"""Plain data model of a guild's permission-relevant structure.

Built from live discord.py objects (see snapshot.py) or from stored JSON backups,
so the engine, auditor and diff logic never need a Discord connection.
"""
from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, field

TEXT_TYPES = {"text", "news", "forum", "media"}
VOICE_TYPES = {"voice", "stage"}


@dataclass
class Role:
    id: int
    name: str
    permissions: int
    position: int
    managed: bool = False
    color: int = 0
    hoist: bool = False
    mentionable: bool = False
    member_count: int | None = None


@dataclass
class Overwrite:
    id: int
    type: str  # "role" | "member"
    allow: int = 0
    deny: int = 0


@dataclass
class Channel:
    id: int
    name: str
    type: str  # text/voice/category/stage/forum/news/media
    parent_id: int | None = None
    position: int = 0
    overwrites: dict[int, Overwrite] = field(default_factory=dict)
    topic: str | None = None
    nsfw: bool = False
    slowmode: int = 0
    bitrate: int | None = None
    user_limit: int | None = None

    @property
    def is_voice(self) -> bool:
        return self.type in VOICE_TYPES

    @property
    def is_text(self) -> bool:
        return self.type in TEXT_TYPES

    @property
    def mention(self) -> str:
        return f"#{self.name}" if self.type != "category" else f"category {self.name}"


@dataclass
class Member:
    id: int
    name: str
    role_ids: list[int] = field(default_factory=list)
    bot: bool = False
    timed_out: bool = False


@dataclass
class Guild:
    id: int
    name: str
    owner_id: int
    roles: dict[int, Role] = field(default_factory=dict)
    channels: dict[int, Channel] = field(default_factory=dict)
    members: dict[int, Member] = field(default_factory=dict)
    settings: dict = field(default_factory=dict)

    # ---- helpers -------------------------------------------------------
    @property
    def everyone(self) -> Role:
        return self.roles[self.id]

    def role_by_name(self, name: str) -> Role | None:
        low = name.lower().lstrip("@")
        for r in self.roles.values():
            if r.name.lower().lstrip("@") == low:
                return r
        return None

    def channels_named(self, name: str) -> list[Channel]:
        low = name.lower().lstrip("#")
        return [c for c in self.channels.values() if c.name.lower() == low]

    def children(self, category_id: int) -> list[Channel]:
        return sorted((c for c in self.channels.values() if c.parent_id == category_id), key=lambda c: c.position)

    def top_role(self, member: Member) -> Role:
        roles = [self.roles[r] for r in member.role_ids if r in self.roles]
        return max(roles, key=lambda r: r.position, default=self.everyone)

    def is_synced(self, channel: Channel) -> bool | None:
        """Mirror of discord.py's permissions_synced: overwrites equal to the parent's."""
        if channel.parent_id is None or channel.parent_id not in self.channels:
            return None
        parent = self.channels[channel.parent_id]
        norm = lambda ows: {k: (o.allow, o.deny) for k, o in ows.items()}  # noqa: E731
        return norm(channel.overwrites) == norm(parent.overwrites)

    def members_with_role(self, role_id: int) -> list[Member]:
        return [m for m in self.members.values() if role_id in m.role_ids]

    def clone(self) -> "Guild":
        return copy.deepcopy(self)

    # ---- serialization -------------------------------------------------
    def to_dict(self, include_members: bool = True) -> dict:
        d = {
            "id": self.id, "name": self.name, "owner_id": self.owner_id, "settings": self.settings,
            "roles": [asdict(r) for r in self.roles.values()],
            "channels": [
                {**{k: v for k, v in asdict(c).items() if k != "overwrites"},
                 "overwrites": [asdict(o) for o in c.overwrites.values()]}
                for c in self.channels.values()
            ],
        }
        if include_members:
            d["members"] = [asdict(m) for m in self.members.values()]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Guild":
        g = cls(id=int(d["id"]), name=d["name"], owner_id=int(d["owner_id"]), settings=d.get("settings", {}))
        for r in d.get("roles", []):
            g.roles[int(r["id"])] = Role(**{**r, "id": int(r["id"])})
        for c in d.get("channels", []):
            ows = {int(o["id"]): Overwrite(**{**o, "id": int(o["id"])}) for o in c.get("overwrites", [])}
            fields = {k: v for k, v in c.items() if k != "overwrites"}
            fields["id"] = int(fields["id"])
            if fields.get("parent_id") is not None:
                fields["parent_id"] = int(fields["parent_id"])
            g.channels[fields["id"]] = Channel(**fields, overwrites=ows)
        for m in d.get("members", []):
            g.members[int(m["id"])] = Member(**{**m, "id": int(m["id"]), "role_ids": [int(x) for x in m["role_ids"]]})
        return g
