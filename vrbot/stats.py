"""Statistics helpers (pure): voice sessions and period aggregation from stored events."""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

VOICE_START = {"voice_join"}
VOICE_END = {"voice_leave", "voice_disconnect"}
VOICE_SWITCH = {"voice_move"}


@dataclass
class Session:
    user_id: int
    name: str
    channel: str
    start: datetime
    end: datetime
    ongoing: bool = False

    @property
    def seconds(self) -> float:
        return max(0.0, (self.end - self.start).total_seconds())


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _det(e: dict) -> dict:
    try:
        return json.loads(e["details"]) if e.get("details") else {}
    except (TypeError, ValueError):
        return {}


def voice_sessions(events: list[dict], now: datetime | None = None, max_hours: float = 12) -> list[Session]:
    """events: voice-category rows (any order). A session = continuous presence in one channel.
    Sessions with no observed end are closed at `now` (ongoing) but capped at max_hours to survive
    missed events (e.g. bot downtime)."""
    now = now or datetime.now(timezone.utc)
    open_: dict[int, tuple[str, datetime, str]] = {}
    out: list[Session] = []
    for e in sorted(events, key=lambda e: (e["ts"], e.get("id", 0))):
        uid = e.get("target_id")
        if uid is None:
            continue
        t, ts, d = e["type"], _dt(e["ts"]), _det(e)
        cur = open_.get(uid)
        if t in VOICE_START or t in VOICE_SWITCH:
            if cur:
                out.append(Session(uid, cur[2], cur[0], cur[1], ts))
            ch = d.get("to_name") or e.get("channel_name") or "?"
            open_[uid] = (ch, ts, e.get("target_name") or str(uid))
        elif t in VOICE_END and cur:
            out.append(Session(uid, cur[2], cur[0], cur[1], ts))
            del open_[uid]
    cap = timedelta(hours=max_hours)
    for uid, (ch, start, name) in open_.items():
        out.append(Session(uid, name, ch, start, min(now, start + cap), ongoing=now - start <= cap))
    return out


def voice_summary(sessions: list[Session], top: int = 5) -> dict:
    by_user = defaultdict(float)
    by_chan = defaultdict(float)
    names = {}
    for s in sessions:
        by_user[s.user_id] += s.seconds
        by_chan[s.channel] += s.seconds
        names[s.user_id] = s.name
    return {
        "sessions": len(sessions),
        "hours": round(sum(s.seconds for s in sessions) / 3600, 1),
        "unique_users": len(by_user),
        "top_users": [(names[u], round(sec / 3600, 1)) for u, sec in sorted(by_user.items(), key=lambda x: -x[1])[:top]],
        "top_channels": [(c, round(sec / 3600, 1)) for c, sec in sorted(by_chan.items(), key=lambda x: -x[1])[:top]],
    }


def per_day(events: list[dict], types: set[str]) -> list[tuple[str, int]]:
    c = Counter(e["ts"][:10] for e in events if e["type"] in types)
    return sorted(c.items())


def guardian_counts(events: list[dict]) -> dict[str, int]:
    out = {"CRITICAL": 0, "WARNING": 0, "INFO": 0}
    for e in events:
        if e["type"] == "guardian_alert":
            sev = _det(e).get("severity")
            if sev in out:
                out[sev] += 1
    return out


def bar(n: int, maxn: int, width: int = 12) -> str:
    return "█" * (round(width * n / maxn) if maxn else 0) or ("▏" if n else "")
