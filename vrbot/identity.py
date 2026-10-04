"""Global identity contract for logs and history.

Names are presentation; the Discord user ID is identity. Every stored event that concerns a person carries a
structured snapshot of that person AT THAT TIME (details.target_identity / details.actor_identity), filled in
centrally by Database.add_event via a resolver — no cog formats identities on its own. Rendered owner logs are a
view of these structured fields and always show the immutable ID.
"""
from __future__ import annotations

from typing import Any


def snapshot(user: Any) -> dict | None:
    """Identity of a discord.User/Member (or anything with .id/.name) at this moment."""
    uid = getattr(user, "id", None)
    if not isinstance(uid, int):
        return None
    out = {"id": uid, "username": getattr(user, "name", None), "global_name": getattr(user, "global_name", None),
           "display_name": getattr(user, "display_name", None), "bot": bool(getattr(user, "bot", False))}
    created = getattr(user, "created_at", None)
    if created is not None:
        out["account_created"] = created.isoformat()
    joined = getattr(user, "joined_at", None)
    if joined is not None:
        out["joined_at"] = joined.isoformat()
    return out


def profile_url(uid: int) -> str:
    return f"https://discord.com/users/{uid}"


def label(ident: dict | None, fallback_name: str | None = None, fallback_id: int | None = None, *, bold: bool = False) -> str:
    """ONE clean clickable identity: '@Name' linking to the user's profile by immutable ID.

    The readable name is presentation; the link target is the Discord user ID (works after renames and after the
    person left, unlike a <@mention>). The raw ID stays in the structured event (Find person shows it on demand)."""
    ident = ident or {}
    uid = ident.get("id") or fallback_id
    name = (ident.get("display_name") or fallback_name or ident.get("username") or "unknown")
    name = name.replace("[", "(").replace("]", ")").replace("\n", " ")[:64]
    text = f"@{name}" + (" (bot)" if ident.get("bot") else "")
    return f"[{text}]({profile_url(uid)})" if uid else text


class IdentityCache:
    """Remembers the last identity seen for each user ID, so a snapshot still exists after someone left."""

    def __init__(self, limit: int = 20000):
        self.data: dict[int, dict] = {}
        self.limit = limit

    def remember(self, ident: dict | None) -> None:
        if ident and ident.get("id"):
            if len(self.data) >= self.limit:
                self.data.pop(next(iter(self.data)))
            self.data[ident["id"]] = ident

    def get(self, uid: int) -> dict | None:
        return self.data.get(uid)
