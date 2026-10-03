"""Read-only full guild inventory over the REST API (no gateway session needed).

Usage: python -m vrbot.cli inventory   -> writes /data/inventory.json and prints a summary.
Only GET requests are made.
"""
from __future__ import annotations

import asyncio
import json

import httpx

from .perms.model import Channel, Guild, Member, Overwrite, Role

API = "https://discord.com/api/v10"
CHANNEL_TYPES = {0: "text", 2: "voice", 4: "category", 5: "news", 13: "stage", 15: "forum", 16: "media"}


async def _get(c: httpx.AsyncClient, path: str, **params):
    for _ in range(5):
        r = await c.get(API + path, params=params or None)
        if r.status_code == 429:
            await asyncio.sleep(float(r.json().get("retry_after", 1)) + 0.2)
            continue
        if r.status_code in (403, 404):
            return {"_error": r.status_code, "_message": r.json().get("message") if r.content else ""}
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"rate limited on {path}")


async def collect(token: str, guild_id: int | None = None) -> dict:
    async with httpx.AsyncClient(timeout=30, headers={"Authorization": f"Bot {token}"}) as c:
        me = await _get(c, "/users/@me")
        app = await _get(c, "/applications/@me")
        guilds = await _get(c, "/users/@me/guilds")
        gid = guild_id or int(guilds[0]["id"])
        g = await _get(c, f"/guilds/{gid}", with_counts="true")
        out = {"bot": {k: me.get(k) for k in ("id", "username", "discriminator", "global_name")},
               "application": {k: app.get(k) for k in ("id", "name", "bot_public", "flags")},
               "guilds_joined": [x["name"] for x in guilds], "guild": g}
        paths = {
            "roles": f"/guilds/{gid}/roles", "channels": f"/guilds/{gid}/channels",
            "threads": f"/guilds/{gid}/threads/active", "webhooks": f"/guilds/{gid}/webhooks",
            "integrations": f"/guilds/{gid}/integrations", "invites": f"/guilds/{gid}/invites",
            "automod": f"/guilds/{gid}/auto-moderation/rules", "events": f"/guilds/{gid}/scheduled-events",
            "onboarding": f"/guilds/{gid}/onboarding", "welcome_screen": f"/guilds/{gid}/welcome-screen",
            "bans": f"/guilds/{gid}/bans", "emojis": f"/guilds/{gid}/emojis", "stickers": f"/guilds/{gid}/stickers",
        }
        for k, p in paths.items():
            out[k] = await _get(c, p)
        members, after = [], "0"
        while True:
            page = await _get(c, f"/guilds/{gid}/members", limit=1000, after=after)
            if not isinstance(page, list) or not page:
                break
            members += page
            after = page[-1]["user"]["id"]
            if len(page) < 1000:
                break
        out["members"] = members
        out["audit_log"] = await _get(c, f"/guilds/{gid}/audit-logs", limit=100)
        return out


def to_model(inv: dict) -> Guild:
    g = inv["guild"]
    gid = int(g["id"])
    m = Guild(id=gid, name=g["name"], owner_id=int(g["owner_id"]), settings={
        k: g.get(k) for k in ("verification_level", "explicit_content_filter", "default_message_notifications",
                              "mfa_level", "afk_channel_id", "afk_timeout", "system_channel_id", "rules_channel_id",
                              "public_updates_channel_id", "premium_tier", "features", "approximate_member_count")})
    counts: dict[int, int] = {}
    for mem in inv["members"]:
        for r in mem["roles"]:
            counts[int(r)] = counts.get(int(r), 0) + 1
    for r in inv["roles"]:
        rid = int(r["id"])
        m.roles[rid] = Role(rid, r["name"], int(r["permissions"]), r["position"], bool(r.get("managed")),
                            r.get("color", 0), r.get("hoist", False), r.get("mentionable", False),
                            len(inv["members"]) if rid == gid else counts.get(rid, 0))
    for ch in inv["channels"]:
        cid = int(ch["id"])
        ows = {int(o["id"]): Overwrite(int(o["id"]), "role" if o["type"] == 0 else "member", int(o["allow"]), int(o["deny"]))
               for o in ch.get("permission_overwrites", [])}
        m.channels[cid] = Channel(cid, ch["name"], CHANNEL_TYPES.get(ch["type"], str(ch["type"])),
                                  int(ch["parent_id"]) if ch.get("parent_id") else None, ch.get("position", 0), ows,
                                  ch.get("topic"), bool(ch.get("nsfw")), ch.get("rate_limit_per_user") or 0,
                                  ch.get("bitrate"), ch.get("user_limit"))
    for mem in inv["members"]:
        u = mem["user"]
        uid = int(u["id"])
        name = mem.get("nick") or u.get("global_name") or u["username"]
        m.members[uid] = Member(uid, name, [int(r) for r in mem["roles"]], bool(u.get("bot")),
                                bool(mem.get("communication_disabled_until")))
    return m


def dump(inv: dict, path) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(inv, f, indent=1, default=str)
