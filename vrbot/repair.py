"""Apply → verify → (rollback) for permission changes. Used by repair, restore and auto-heal.

Safety properties:
* A pre-change snapshot is always stored first.
* Every change carries its `before` state, persisted in change_batches, so it can be rolled back.
* After applying, live state is re-read and each change is verified individually.
* Rollback refuses to overwrite objects that changed again since the batch (drift), unless forced.
"""
from __future__ import annotations

import logging

import discord

from .events import bot_reason
from .perms.audit import Change

log = logging.getLogger("vrbot.repair")


class LiveState:
    """Authoritative state read straight from Discord's REST API (not the gateway cache)."""

    def __init__(self, roles: dict[int, int], overwrites: dict[int, dict[int, list[int]]]):
        self.roles = roles
        self.overwrites = overwrites

    @classmethod
    async def fetch(cls, bot) -> "LiveState":
        gid = bot.guild.id
        roles = {int(r["id"]): int(r["permissions"]) for r in await bot.http.get_roles(gid)}
        ows: dict[int, dict[int, list[int]]] = {}
        for ch in await bot.http.get_all_guild_channels(gid):
            ows[int(ch["id"])] = {int(o["id"]): [int(o["allow"]), int(o["deny"])]
                                  for o in ch.get("permission_overwrites", [])}
        return cls(roles, ows)

    def value(self, c: Change):
        if c.kind == "role_perms":
            return self.roles.get(c.target_id)
        if c.channel_id not in self.overwrites:
            return "missing-channel"
        return self.overwrites[c.channel_id].get(c.target_id)


def _norm(v):
    return list(v) if isinstance(v, (list, tuple)) else v


async def _apply_one(bot, guild: discord.Guild, c: Change, reason: str) -> None:
    if c.kind == "role_perms":
        role = guild.get_role(c.target_id)
        if role is None:
            raise RuntimeError(f"role {c.target_id} not found")
        await role.edit(permissions=discord.Permissions(int(c.after)), reason=reason)
        return
    if c.after is None:
        await bot.http.delete_channel_permissions(c.channel_id, c.target_id, reason=reason)
    else:
        allow, deny = c.after
        await bot.http.edit_channel_permissions(c.channel_id, c.target_id, str(allow), str(deny),
                                                0 if c.target_type == "role" else 1, reason=reason)


async def apply_changes(bot, changes: list[Change], *, source: str, actor: discord.abc.User | None,
                        summary: str) -> dict:
    guild = bot.guild
    bot.guard("permission_batch", actor.id if actor else None, len(changes))  # safe mode + batch size limit
    snap_id = await bot.take_snapshot("pre-change", label=f"before {source}", created_by=actor.id if actor else None)
    batch_id = await bot.db.add_batch(guild.id, actor.id if actor else None, source, "planned", summary,
                                      [c.to_dict() for c in changes], snapshot_id=snap_id)
    reason = bot_reason(actor.display_name if actor else "auto-heal", actor.id if actor else bot.user.id,
                        f"{source} batch #{batch_id}")
    errors: dict[int, str] = {}
    for i, c in enumerate(changes):
        try:
            await _apply_one(bot, guild, c, reason)
        except discord.HTTPException as e:
            errors[i] = f"{e.status} {e.text or e}"
        except Exception as e:  # noqa: BLE001
            errors[i] = f"{type(e).__name__}: {e}"
    # verify: re-read Discord over REST and compare every change individually
    live = await LiveState.fetch(bot)
    verified, mismatched = [], []
    for i, c in enumerate(changes):
        cur = live.value(c)
        if _norm(cur) == _norm(c.after):
            verified.append(i)
        else:
            mismatched.append({"index": i, "change": c.describe(), "now": cur, "error": errors.get(i)})
    status = "applied" if not mismatched else ("partial" if verified else "failed")
    result = {"verified": len(verified), "mismatched": mismatched, "errors": errors}
    await bot.db.update_batch(batch_id, status, result)
    await bot.db.add_event(type="repair_applied" if source != "autoheal" else "autoheal", category="bot",
                           guild_id=guild.id, actor_id=actor.id if actor else bot.user.id,
                           actor_name=actor.display_name if actor else "the bot", actor_confidence="confirmed",
                           reason=summary, details={"batch": batch_id, "status": status, "changes": len(changes),
                                                    "verified": len(verified)}, source="bot")
    return {"batch_id": batch_id, "snapshot_id": snap_id, "status": status, **result}


async def rollback_batch(bot, batch_id: int, actor: discord.abc.User, force: bool = False) -> dict:
    b = await bot.db.get_batch(batch_id)
    if not b:
        return {"error": f"Batch #{batch_id} not found."}
    if b["status"] == "rolled_back":
        return {"error": f"Batch #{batch_id} was already rolled back."}
    live = await LiveState.fetch(bot)
    reverse, drifted = [], []
    for d in b["changes"]:
        c = Change.from_dict(d)
        if _norm(live.value(c)) != _norm(c.after) and not force:
            drifted.append(c.describe())
            continue
        reverse.append(Change(c.kind, c.target_id, c.target_name, c.channel_id, c.channel_name, c.target_type,
                              before=c.after, after=c.before))
    if not reverse:
        return {"error": "Nothing to roll back safely (everything changed again since).", "drifted": drifted}
    res = await apply_changes(bot, reverse, source="rollback", actor=actor, summary=f"rollback of batch #{batch_id}")
    if res["status"] == "applied":
        await bot.db.update_batch(batch_id, "rolled_back", {"rollback_batch": res["batch_id"]})
    res["drifted"] = drifted
    return res
