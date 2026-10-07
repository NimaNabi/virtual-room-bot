"""Safe diagnostics report for bug reports ("it doesn't work").

Contains versions, platform, runtime state, database migration level, module status and recent errors — after
sanitising. It NEVER contains the token, API keys, passwords, message content, the database or member lists; Discord
IDs, e-mail addresses and anything token-like are masked. Safe to attach to a public GitHub issue.
"""
from __future__ import annotations

import json
import os
import platform
import re
import sqlite3
import sys
import time
from pathlib import Path

SECRET_KEYS = ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "APIKEY", "KEY")
_PATTERNS = [
    (re.compile(r"[MNO][A-Za-z\d_-]{23,27}\.[A-Za-z\d_-]{6}\.[A-Za-z\d_-]{27,40}"), "<token>"),
    (re.compile(r"(?i)\b(bearer|authorization)[:=\s]+\S+"), r"\1 <redacted>"),
    (re.compile(r"(?i)\b(sk|gh[pousr]|xox[abpr])[-_][A-Za-z0-9_-]{10,}"), "<key>"),
    (re.compile(r"(?i)\b([A-Z_]*(?:TOKEN|SECRET|PASSWORD|API_KEY))\s*[=:]\s*\S+"), r"\1=<redacted>"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "<email>"),
    (re.compile(r"(?<!\d)\d{17,20}(?!\d)"), "<id>"),
    (re.compile(r'"(content|before|after|excerpt)":\s*"(?:[^"\\]|\\.)*"'), r'"\1": "<message text removed>"'),
]


def sanitize(text: str) -> str:
    for pat, rep in _PATTERNS:
        text = pat.sub(rep, text)
    return text


def recent_problems(lines: list[str], limit: int = 25) -> list[str]:
    """WARNING/ERROR/CRITICAL lines (JSON logs or plain text), sanitised, newest last."""
    out = []
    for raw in lines:
        try:
            d = json.loads(raw)
            if d.get("level") in ("WARNING", "ERROR", "CRITICAL"):
                out.append(f"{d.get('ts', '')} {d['level']} {d.get('logger', '')}: {d.get('msg', '')}"[:400])
        except (ValueError, AttributeError):
            if re.search(r"\b(ERROR|CRITICAL|Traceback|Exception)\b", raw):
                out.append(raw[:400])
    return [sanitize(x) for x in out[-limit:]]


def report(data_dir: Path, app_dir: Path, log_lines: list[str] | None = None, extra: dict | None = None) -> str:
    v = (app_dir / "VERSION").read_text().strip() if (app_dir / "VERSION").exists() else "dev"
    lines = ["# Diagnostics report", f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S %z')}", "",
             f"Version: {v}", f"OS: {platform.platform()}", f"Python: {sys.version.split()[0]} ({platform.machine()})"]
    hb = {}
    try:
        hb = json.loads((data_dir / "heartbeat.json").read_text())
    except (OSError, ValueError):
        pass
    age = int(time.time() - hb.get("ts", 0)) if hb else None
    lines += [f"Bot state: {hb.get('state', 'no heartbeat')}" + (f" ({age} s ago)" if age is not None else ""),
              f"Latency: {hb.get('latency_ms')} ms · Safe mode: {hb.get('safe_mode')} · Uptime: {hb.get('uptime_s')} s"]
    mods = hb.get("modules") or {}
    if mods:
        bad = {k: x for k, x in mods.items() if x not in ("loaded", "ok")}
        lines.append(f"Modules: {len(mods) - len(bad)} ok" + (f"; problems: {sanitize(json.dumps(bad))}" if bad else ""))
    db = next((p for p in (data_dir / "vrbot.db", data_dir / "vrbot.db") if p.exists()), None)
    if db:
        try:
            c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            mig = [r[0] for r in c.execute("SELECT version FROM schema_version ORDER BY version")]
            n = c.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            last = {k: c.execute("SELECT value FROM kv WHERE key=?", (k,)).fetchone()
                    for k in ("backups:last", "uptime:last_downtime")}
            c.close()
            lines.append(f"Database: migrations {mig}, {n} events, {db.stat().st_size // 1024} KB")
            for k, r in last.items():
                if r:
                    lines.append(f"{k}: {sanitize(r[0])[:300]}")
        except sqlite3.Error as e:
            lines.append(f"Database: unreadable ({type(e).__name__})")
    else:
        lines.append("Database: not created yet")
    wd = data_dir / "watchdog.json"
    if wd.exists():
        lines.append(f"Last watchdog restart: {sanitize(wd.read_text())[:300]}")
    env = {k: ("set" if os.environ.get(k) else "not set") for k in ("DISCORD_TOKEN", "LAVALINK_PASSWORD", "AI_API_KEY")}
    lines.append("Secrets: " + ", ".join(f"{k} {s}" for k, s in env.items()) + " (values never included)")
    lines.append(f"Music: LAVALINK_URI {'set' if os.environ.get('LAVALINK_URI') else 'not set'}; "
                 f"Message Content intent: {os.environ.get('MESSAGE_CONTENT_INTENT', 'false')}")
    for k, val in (extra or {}).items():
        lines.append(f"{k}: {sanitize(str(val))}")
    probs = recent_problems(log_lines or [])
    lines += ["", f"Recent warnings/errors ({len(probs)}):", *(probs or ["none found" if log_lines else "no log provided"])]
    lines += ["", "This report contains no token, keys, message text or member data. Review it before posting."]
    return "\n".join(lines)
