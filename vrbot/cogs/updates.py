"""Owner-only update notifications. Notify, never self-install.

The running bot never replaces its own code. It only:
  * reads the installed version (VERSION) and the host updater's state (/data/update-state.json);
  * learns the latest version from the host updater (`scripts/update.sh check` writes /data/update-check.json) or,
    if the owner set UPDATE_REPO (owner/name of the public repository; an optional UPDATE_GITHUB_TOKEN is only
    needed for a private fork), from the public GitHub API;
  * tells the owner once per new version (owner-only alert channel) and shows release notes;
  * records an update request (/data/update-request.json) that the HOST updater applies with backup, health check
    and rollback (`scripts/update.sh apply` or `scripts/update.sh watch`).
With nothing configured it makes no network calls at all.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import discord
from discord.ext import commands, tasks

log = logging.getLogger("vrbot.updates")
APP = Path(__file__).resolve().parent.parent.parent


def parse_version(v: str | None) -> tuple[int, int, int] | None:
    try:
        a, b, c = (v or "").strip().lstrip("v").split(".")[:3]
        return int(a), int(b), int(c)
    except ValueError:
        return None


def is_newer(latest: str | None, current: str | None) -> bool:
    lv, cv = parse_version(latest), parse_version(current)
    return bool(lv and cv and lv > cv)


def kind_of(latest: str, current: str) -> str:
    lv, cv = parse_version(latest), parse_version(current)
    if not lv or not cv:
        return "unknown"
    return "major (may need config changes)" if lv[0] > cv[0] else "feature" if lv[1] > cv[1] else "bug-fix"


class Updates(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.data = Path(os.environ.get("DATA_DIR", "/data"))

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.daily.is_running():  # start only once connected (never during offline loading)
            self.daily.start()

    def cog_unload(self):
        self.daily.cancel()

    # ---------------------------------------------------------- state
    def current(self) -> str:
        try:
            return (APP / "VERSION").read_text(encoding="utf-8").strip()
        except OSError:
            return "unknown"

    def _json(self, name: str) -> dict:
        try:
            return json.loads((self.data / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def state(self) -> dict:
        """Host updater state: current/previous version, last successful update, last result."""
        return self._json("update-state.json")

    def latest(self) -> dict:
        """{'version', 'notes', 'breaking', 'checked_at', 'source'} from the host check or the GitHub API check."""
        return self._json("update-check.json")

    def pending_request(self) -> dict:
        return self._json("update-request.json")

    def install_kind(self) -> str:
        return self.state().get("install") or "unknown (run scripts/update.sh check on the host)"

    async def check_github(self) -> dict | None:
        """Only when the owner set UPDATE_REPO. Public repositories need no credential; a private fork can use the
        owner's OWN read-only UPDATE_GITHUB_TOKEN. Never a built-in credential."""
        repo, token = os.environ.get("UPDATE_REPO", "").strip(), os.environ.get("UPDATE_GITHUB_TOKEN", "").strip()
        if not repo:
            return None
        import httpx
        h = {"Accept": "application/vnd.github+json"}
        if token:
            h["Authorization"] = f"Bearer {token}"
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(f"https://api.github.com/repos/{repo}/tags", headers=h)
            r.raise_for_status()
            tags = [t["name"] for t in r.json() if parse_version(t["name"])]
            if not tags:
                return None
            newest = max(tags, key=parse_version)
            meta = {}
            rr = await c.get(f"https://api.github.com/repos/{repo}/contents/release.json", params={"ref": newest},
                             headers={**h, "Accept": "application/vnd.github.raw"})
            if rr.status_code == 200:
                meta = rr.json()
        info = {"version": newest.lstrip("v"), "notes": meta.get("notes", []), "breaking": meta.get("breaking", False),
                "min_config_schema": meta.get("min_config_schema"), "checked_at": time.time(), "source": "github"}
        (self.data / "update-check.json").write_text(json.dumps(info), encoding="utf-8")
        return info

    @tasks.loop(hours=24)
    async def daily(self):
        """Low-frequency check; tells the owner once per new version, otherwise silent."""
        try:
            await self.check_github()
        except Exception as e:  # noqa: BLE001
            log.warning("update check failed: %s", type(e).__name__)
        latest = self.latest().get("version")
        if not is_newer(latest, self.current()):
            return
        if await self.bot.db.kv_get("updates:notified") == latest:
            return
        await self.bot.db.kv_set("updates:notified", latest)
        guardian = self.bot.get_cog("Guardian")
        if guardian:
            await guardian.alert("INFO", f"Update available: {latest}", f"Installed: {self.current()}. "
                                 "Open 🎛️ → Owner → Updates for release notes.", key=f"update:{latest}",
                                 kind="Updates", post=True)

    @daily.before_loop
    async def _wait(self):
        await self.bot.wait_until_ready()

    # ---------------------------------------------------------- owner UI support
    def summary(self) -> tuple[discord.Embed, bool]:
        cur, st, lt = self.current(), self.state(), self.latest()
        latest = lt.get("version")
        newer = is_newer(latest, cur)
        lines = [f"**Installed:** {cur}", f"**Latest known:** {latest or 'not checked yet'}"]
        if newer:
            lines.append(f"🆕 **Update available** ({kind_of(latest, cur)})" + (" — ⚠️ breaking" if lt.get("breaking") else ""))
        elif latest:
            lines.append("✅ Up to date")
        if st.get("previous"):
            lines.append(f"**Previous version:** {st['previous']}")
        if st.get("last_success"):
            lines.append(f"**Last successful update:** <t:{int(st['last_success'])}:f>")
        if st.get("last_result") and st.get("last_result") != "ok":
            lines.append(f"**Last attempt:** {st['last_result']}")
        req = self.pending_request()
        if req:
            lines.append(f"⏳ **Update to {req.get('version')} requested** — waiting for the host updater.")
        if lt.get("checked_at"):
            lines.append(f"_Checked <t:{int(lt['checked_at'])}:R> via {lt.get('source', 'host')}_")
        emb = discord.Embed(title="⚙️ Updates", description="\n".join(lines), color=0x5865F2)
        emb.add_field(name="How updates are applied", inline=False, value=(
            "The bot never updates itself. Updates run on the host with a backup, a health check and automatic "
            "rollback: `bash scripts/update.sh apply` (Git installs). ZIP installs: get the new package and follow "
            "the README update steps."))
        return emb, newer

    def notes(self) -> str:
        lt = self.latest()
        items = lt.get("notes") or []
        return "\n".join(f"• {n}" for n in items) or "No release notes available yet."

    def request(self, version: str, user_id: int) -> None:
        (self.data / "update-request.json").write_text(json.dumps({"version": version, "requested_by": user_id,
                                                                   "ts": time.time()}), encoding="utf-8")


async def setup(bot):
    await bot.add_cog(Updates(bot))
