"""Auto-heal decision policy (pure).

Auto-heal repairs only unambiguous drift from the APPROVED baseline, and only when:
* the plan is small (≤ max_changes), simulated-safe (no new problems, not blocked) and grants nothing dangerous;
* the exact same drift was seen on the previous check at least `grace` seconds ago (not a change in progress);
* nobody with owner authority made the change (owner edits are intentional → alert, never auto-revert).
"""
from __future__ import annotations

from datetime import datetime


def decide(plan_keys: list[str], pending: dict | None, now: datetime, *, safe: bool, dangerous: bool,
           n_changes: int, max_changes: int, owner_touched: bool, grace_s: float = 600) -> tuple[str, dict | None]:
    if not plan_keys:
        return "none", None
    if dangerous or not safe or n_changes > max_changes:
        return "skip_unsafe", None
    if owner_touched:
        return "skip_owner", None
    keys = sorted(plan_keys)
    if pending and pending.get("keys") == keys:
        first = datetime.fromisoformat(pending["first"])
        if (now - first).total_seconds() >= grace_s:
            return "apply", None
        return "wait", pending
    return "wait", {"keys": keys, "first": now.isoformat()}
