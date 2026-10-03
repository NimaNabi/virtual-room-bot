"""Operator CLI: python -m vrbot.cli <health|invite|db-stats|check-config>"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import time
from pathlib import Path

from .perms import flags as F
from .perms.audit import BOT_REQUIRED

INVITE_PERMS = BOT_REQUIRED + ["send_messages_in_threads", "add_reactions", "use_external_emojis"]


def permissions_integer() -> int:
    return F.value_of(INVITE_PERMS)


def client_id_from_token(token: str) -> str:
    part = token.split(".")[0]
    return base64.b64decode(part + "=" * (-len(part) % 4)).decode()


def invite_url(client_id: str) -> str:
    return (f"https://discord.com/oauth2/authorize?client_id={client_id}"
            f"&permissions={permissions_integer()}&integration_type=0&scope=bot+applications.commands")


def health() -> int:
    p = Path(os.environ.get("DATA_DIR", "/data")) / "heartbeat.json"
    try:
        d = json.loads(p.read_text())
    except (OSError, ValueError):
        print("no heartbeat")
        return 1
    age = time.time() - d.get("ts", 0)
    ok = age < 120 and d.get("state") == "ready"
    print(f"state={d.get('state')} age={int(age)}s guild={d.get('guild')} latency={d.get('latency_ms')}ms")
    return 0 if ok else 1


async def verify() -> int:
    """Live checks of the non-Discord dependencies from inside the container."""
    import httpx

    from .config import Settings
    s = Settings.from_env()
    ok = True
    async with httpx.AsyncClient(timeout=60) as c:
        if s.lavalink_uri and s.lavalink_password:
            h = {"Authorization": s.lavalink_password}
            try:
                v = await c.get(f"{s.lavalink_uri}/version", headers=h)
                print(f"[lavalink] version {v.text} (HTTP {v.status_code})")
                for ident in ("ytsearch:lofi hip hop", "scsearch:lofi hip hop"):
                    r = (await c.get(f"{s.lavalink_uri}/v4/loadtracks", params={"identifier": ident}, headers=h)).json()
                    n = len(r.get("data") or []) if r.get("loadType") == "search" else 0
                    first = (r.get("data") or [{}])[0].get("info", {}).get("title") if n else r.get("data")
                    print(f"[lavalink] {ident.split(':')[0]}: loadType={r.get('loadType')} results={n} first={str(first)[:70]!r}")
                    ok &= ident.startswith("sc") and n > 0 or not ident.startswith("sc")
            except httpx.HTTPError as e:
                print(f"[lavalink] FAILED {e!r}")
                ok = False
        else:
            print("[lavalink] not configured")
        if s.ai_base_url:
            from .ai.client import AIUnavailable, ChatClient
            cl = ChatClient(s.ai_base_url, s.ai_api_key, [s.ai_model] + s.ai_fallback_models)
            print(f"[ai] gateway reachable: {await cl.health()}")
            tool = [{"type": "function", "function": {"name": "server_status", "description": "Get member count",
                                                      "parameters": {"type": "object", "properties": {}}}}]
            try:
                msg = await cl.complete([{"role": "user", "content": "How many members does the server have? Use the tool."}], tool)
                calls = [tc["function"]["name"] for tc in msg.get("tool_calls") or []]
                print(f"[ai] model={cl.last_model} tool_calls={calls or 'none'}")
                ok &= bool(calls)
            except AIUnavailable as e:
                print(f"[ai] FAILED {e}")
                ok = False
            await cl.close()
        else:
            print("[ai] not configured")
    print("VERIFY:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "help"
    if cmd == "health":
        return health()
    if cmd == "invite":
        cid = argv[2] if len(argv) > 2 else None
        if not cid and os.environ.get("DISCORD_TOKEN"):
            cid = client_id_from_token(os.environ["DISCORD_TOKEN"])
        if not cid:
            print("usage: invite <application_id>  (or set DISCORD_TOKEN)")
            return 2
        print(invite_url(cid))
        print(f"permissions integer: {permissions_integer()} = {', '.join(INVITE_PERMS)}")
        return 0
    if cmd == "db-stats":
        from .db import Database

        async def run():
            db = Database(Path(os.environ.get("DATA_DIR", "/data")) / "vrbot.db")
            await db.open()
            print(json.dumps(await db.stats()))
            await db.close()
        asyncio.run(run())
        return 0
    if cmd == "inventory":
        from .inventory import collect, dump
        inv = asyncio.run(collect(os.environ["DISCORD_TOKEN"], int(os.environ["GUILD_ID"]) if os.environ.get("GUILD_ID") else None))
        p = Path(os.environ.get("DATA_DIR", "/data")) / "inventory.json"
        dump(inv, p)
        print(f"wrote {p}: {len(inv['roles'])} roles, {len(inv['channels'])} channels, {len(inv['members'])} members")
        return 0
    if cmd == "backup":
        import sqlite3
        import time as _t
        data = Path(os.environ.get("DATA_DIR", "/data"))
        (data / "backups").mkdir(parents=True, exist_ok=True)
        out = data / "backups" / f"vrbot-{_t.strftime('%Y%m%d-%H%M%S')}.db"
        src, dst = sqlite3.connect(data / "vrbot.db"), sqlite3.connect(out)
        src.backup(dst)
        dst.close()
        src.close()
        print(f"backup written: {out}")
        return 0
    if cmd == "verify":
        return asyncio.run(verify())
    if cmd == "check-config":
        from .config import load_server_config
        cfg = load_server_config(os.environ.get("CONFIG_PATH", "/data/server.yaml"))
        for rule in cfg.baseline.channels:
            for k in list(cfg.baseline.trust_levels) + ["default"]:
                rule.expectations(k)
        print(f"OK: {len(cfg.baseline.trust_levels)} trust levels, {len(cfg.baseline.channels)} channel rules")
        return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
