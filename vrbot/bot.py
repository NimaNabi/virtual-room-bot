"""ServerBot core: client, shared services, module loading, background tasks."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from .authz import Level, level_for
from .config import ServerConfig, Settings, load_server_config
from .db import Database, now_iso
from .events import AuditBuffer, VoiceAttributor
from .perms import flags as F
from .perms.snapshot import from_discord

log = logging.getLogger("vrbot")


class MutationRefused(app_commands.CheckFailure):
    """Safe mode / rate limit / batch limit refusal (message is shown to the user)."""


MODULES = [
    "vrbot.cogs.eventlog",
    "vrbot.cogs.guardian",
    "vrbot.cogs.stats",
    "vrbot.cogs.voicerooms",
    "vrbot.cogs.help",
    "vrbot.cogs.ownerlogs",
    "vrbot.cogs.zero",
    "vrbot.cogs.invites",
    "vrbot.cogs.tier",
    "vrbot.cogs.access",
    "vrbot.cogs.guests",
    "vrbot.cogs.fun",
    "vrbot.cogs.presence",
    "vrbot.cogs.app",
    "vrbot.cogs.onboarding",
    "vrbot.cogs.logs",
    "vrbot.cogs.permissions",
    "vrbot.cogs.server",
    "vrbot.cogs.baseline",
    "vrbot.cogs.backup",
    "vrbot.cogs.moderation",
    "vrbot.cogs.welcome",
    "vrbot.cogs.music",
    "vrbot.cogs.ai",
    "vrbot.cogs.setup",
    "vrbot.cogs.updates",
]


class ServerBot(commands.Bot):
    def __init__(self, settings: Settings):
        intents = discord.Intents.none()
        intents.guilds = True
        intents.members = True            # privileged: joins/leaves/role changes/member list
        intents.voice_states = True       # voice join/leave/move/mute metadata
        intents.moderation = True         # bans + GUILD_AUDIT_LOG_ENTRY_CREATE
        intents.guild_messages = True     # delete/edit metadata (no content unless enabled)
        intents.message_content = settings.message_content_intent  # privileged, off by default
        intents.presences = settings.presence_intent              # privileged, off by default (owner presence log)
        super().__init__(command_prefix=commands.when_mentioned, intents=intents, help_command=None,
                         allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True),
                         member_cache_flags=discord.MemberCacheFlags.all())
        self.settings = settings
        self.cfg: ServerConfig = load_server_config(settings.config_path, settings)
        self.db = Database(settings.data_dir / "vrbot.db")
        from .identity import IdentityCache, snapshot
        self.identities = IdentityCache()

        def _resolve(uid: int):
            """Identity snapshot for a user ID: live member/user cache first, else the last identity seen."""
            g = self.guild
            u = (g.get_member(uid) if g else None) or self.get_user(uid)
            if u is not None:
                ident = snapshot(u)
                self.identities.remember(ident)
                return ident
            return self.identities.get(uid)
        self.db.identity_resolver = _resolve
        self.audit_buffer = AuditBuffer()
        self.voice_attr = VoiceAttributor()
        self.module_status: dict[str, str] = {}
        self.started = time.time()
        self.ready_once = asyncio.Event()
        self._mod_rate: dict[int, list[float]] = {}
        self._last_safe: bool | None = None

    # ------------------------------------------------------------ lifecycle
    async def setup_hook(self) -> None:
        await self.db.open()
        for mod in MODULES:
            try:
                await self.load_extension(mod)
                self.module_status[mod.rsplit(".", 1)[1]] = "loaded"
            except Exception as e:  # noqa: BLE001 — one broken module must not stop the bot
                self.module_status[mod.rsplit(".", 1)[1]] = f"failed: {type(e).__name__}: {e}"
                log.exception("module %s failed to load", mod)
        self.tree.on_error = self.on_app_command_error
        self.heartbeat.start()

    async def on_ready(self) -> None:
        g = self.guild
        log.info("connected as %s (%s); guilds=%s target=%s", self.user, self.user.id,
                 [x.name for x in self.guilds], g.name if g else None)
        if g is None:
            log.error("No target guild. Set GUILD_ID or invite the bot to exactly one server.")
            return
        try:
            await self.refresh_presence()
        except Exception:  # noqa: BLE001
            log.warning("presence update failed", exc_info=True)
        if not self.ready_once.is_set():
            try:
                self.tree.copy_global_to(guild=g)
                synced = await self.tree.sync(guild=g)
                log.info("synced %d slash commands to %s", len(synced), g.name)
            except Exception:  # noqa: BLE001
                log.exception("command sync failed")
            self.ready_once.set()
            if not self.maintenance.is_running():
                self.maintenance.start()
            await self.db.add_event(type="bot_started", category="bot", guild_id=g.id,
                                    details={"modules": self.module_status}, source="bot")

    async def close(self) -> None:
        try:
            self._write_heartbeat(state="stopping")
        finally:
            await super().close()
            await self.db.close()

    # ------------------------------------------------------------ safety
    def safe_mode(self) -> bool:
        """READ-ONLY kill switch: env SAFE_MODE=true, config security.safe_mode, or a data/SAFE_MODE file."""
        return (os.environ.get("SAFE_MODE", "").lower() == "true" or self.cfg.security.safe_mode
                or (self.settings.data_dir / "SAFE_MODE").exists())

    def guard(self, kind: str, actor_id: int | None, count: int = 1) -> None:
        """Call before EVERY server mutation. Raises MutationRefused (shown to the user) when not allowed."""
        if self.safe_mode():
            raise MutationRefused("🛑 The bot is in **SAFE MODE** (read-only). Monitoring continues; "
                                  "no server changes are made. Owner: `/guardian safemode off`.")
        if kind in ("kick", "ban", "timeout") and actor_id:
            now = time.time()
            win = [t for t in self._mod_rate.get(actor_id, []) if now - t < 600]
            if len(win) + count > self.cfg.security.max_mod_actions_per_10min:
                raise MutationRefused(f"Rate limit: at most {self.cfg.security.max_mod_actions_per_10min} kicks/bans/"
                                      "timeouts per 10 minutes per moderator. This protects against mistakes and "
                                      "compromised accounts.")
            self._mod_rate[actor_id] = win + [now] * count
        if kind == "permission_batch" and count > self.cfg.security.max_changes_per_batch:
            raise MutationRefused(f"{count} changes in one batch exceeds the safety limit of "
                                  f"{self.cfg.security.max_changes_per_batch}. Repair one trust level at a time.")

    async def refresh_presence(self) -> None:
        text = self.cfg.identity.control_center_name + (" · safe mode" if self.safe_mode() else "")
        await self.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name=text),
                                   status=discord.Status.dnd if self.safe_mode() else discord.Status.online)

    # ------------------------------------------------------------ helpers
    @property
    def guild(self) -> discord.Guild | None:
        if self.settings.guild_id:
            return self.get_guild(self.settings.guild_id)
        return self.guilds[0] if len(self.guilds) == 1 else None

    def bot_owner_ids(self) -> set[int]:  # NB: commands.Bot already has an `owner_ids` attribute
        # approved owner-equivalents (explicit IDs from OWNER_EQUIVALENT_IDS / config) get owner level too
        return set(self.settings.owner_ids) | set(self.cfg.access.owner_ids) | set(self.cfg.privacy.approved_owner_equivalents)

    def role_ids(self, names: list[str]) -> set[int]:
        g = self.guild
        if not g:
            return set()
        out = set()
        for n in names:
            r = (g.get_role(int(n)) if n.isdigit() else None) or discord.utils.find(lambda r: r.name.lower() == n.lower(), g.roles)
            if r:
                out.add(r.id)
        return out

    def trust_level_role_ids(self) -> dict[str, int]:
        g = self.guild
        if not g:
            return {}
        model_roles = {}
        for key, c in self.cfg.baseline.trust_levels.items():
            r = (g.get_role(int(c.role)) if c.role.isdigit() else None) or discord.utils.find(
                lambda r: r.name.lower() == c.role.lower(), g.roles)
            if r:
                model_roles[key] = r.id
        return model_roles

    def level_of(self, member: discord.Member | discord.User) -> Level:
        g = self.guild
        if g is None or not isinstance(member, discord.Member):
            return Level.OWNER if member.id in self.bot_owner_ids() else Level.MEMBER
        perms = member.guild_permissions
        trust_levels = self.trust_level_role_ids()
        trusted = set(trust_levels.values())  # every Level is a trusted tier (privacy floors decide what they see)
        return level_for(
            user_id=member.id, role_ids={r.id for r in member.roles}, guild_owner_id=g.owner_id,
            owner_ids=self.bot_owner_ids(), admin_role_ids=self.role_ids(self.cfg.access.admin_roles),
            mod_role_ids=self.role_ids(self.cfg.access.moderator_roles), trusted_role_ids=trusted,
            has_administrator=perms.administrator,
            has_mod_perms=perms.moderate_members or perms.kick_members or perms.ban_members,
        )

    def reload_config(self) -> ServerConfig:
        self.cfg = load_server_config(self.settings.config_path, self.settings)
        return self.cfg

    def model(self, include_members: bool = True):
        return self.annotate(from_discord(self.guild, include_members=include_members))

    def annotate(self, m):
        """Bot-managed runtime state every model needs (used by the gateway and REST-built models alike)."""
        # active bot-managed temporary passes (e.g. /owner_room bring) are not privacy leaks
        m.settings["temporary_passes"] = [list(x) for x in getattr(self, "temporary_passes", set())]
        # bot-managed temporary authority (never trust): recognised by the auditor / privacy engine
        m.settings["temporary_admins"] = sorted(getattr(self, "temporary_admins", set()))
        m.settings["elevation_roles"] = sorted(getattr(self, "elevation_role_ids", set()))
        return m

    async def take_snapshot(self, kind: str, label: str | None = None, created_by: int | None = None) -> int:
        g = self.guild
        m = self.model(include_members=True)
        data = m.to_dict(include_members=False)
        # keep only member ids referenced by member overwrites (needed for restore); no member list archiving
        ow_members = {tid for c in m.channels.values() for tid, o in c.overwrites.items() if o.type == "member"}
        data["members"] = [{"id": mm.id, "name": mm.name, "role_ids": mm.role_ids, "bot": mm.bot, "timed_out": False}
                           for mm in m.members.values() if mm.id in ow_members or mm.id == g.me.id]
        summary = f"{len(m.roles)} roles, {len(m.channels)} channels"
        return await self.db.add_snapshot(g.id, kind, data, label=label, created_by=created_by, summary=summary)

    async def post_log(self, event_type: str, embed: discord.Embed) -> None:
        if event_type not in self.cfg.post_events or not self.cfg.log_channel or not self.guild:
            return
        ch = self.find_text_channel(self.cfg.log_channel)
        if ch:
            try:
                await ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                log.warning("cannot post to log channel %s", ch)

    def find_text_channel(self, ref: str) -> discord.TextChannel | None:
        g = self.guild
        if not g or not ref:
            return None
        ref = ref.lstrip("#")
        if ref.isdigit():
            c = g.get_channel(int(ref))
            return c if isinstance(c, discord.TextChannel) else None
        return discord.utils.find(lambda c: c.name.lower() == ref.lower(), g.text_channels)

    # ------------------------------------------------------------ errors
    async def on_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        orig = getattr(error, "original", error)
        if isinstance(error, app_commands.CheckFailure) or isinstance(orig, app_commands.CheckFailure):
            msg = str(orig if isinstance(orig, app_commands.CheckFailure) else error) or "You are not allowed to use this command."
            # denied attempts are security-relevant (Guardian watches for repeats)
            cmd = interaction.command.qualified_name if interaction.command else "?"
            if self.guild and cmd.split(" ")[0] in ("mod", "baseline", "backup", "guardian", "voice", "server", "logs", "permissions", "stats"):
                try:
                    await self.db.add_event(type="command_denied", category="security", guild_id=self.guild.id,
                                            actor_id=interaction.user.id, actor_name=interaction.user.display_name,
                                            actor_confidence="confirmed", reason=msg[:300], details={"command": cmd},
                                            source="bot")
                    guardian = self.get_cog("Guardian")
                    if guardian:
                        await guardian.on_command_denied(interaction.user, cmd)
                except Exception:  # noqa: BLE001
                    log.warning("could not record denied command", exc_info=True)
        elif isinstance(orig, discord.Forbidden):
            msg = f"Discord refused: missing permissions or role hierarchy ({orig.text or orig})."
        else:
            log.exception("command error in /%s", interaction.command.qualified_name if interaction.command else "?",
                          exc_info=orig)
            msg = f"Something went wrong: `{type(orig).__name__}`. It was logged."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(f"⚠️ {msg}", ephemeral=True)
            else:
                await interaction.response.send_message(f"⚠️ {msg}", ephemeral=True)
        except discord.HTTPException:
            pass

    # ------------------------------------------------------------ tasks
    def _write_heartbeat(self, state: str | None = None) -> None:
        data = {
            "ts": time.time(), "iso": now_iso(),
            "state": state or ("ready" if self.is_ready() and self.guild else "connecting"),
            "safe_mode": self.safe_mode(),
            "latency_ms": round(self.latency * 1000) if self.latency == self.latency else None,
            "guild": self.guild.name if self.guild else None,
            "modules": self.module_status, "uptime_s": round(time.time() - self.started),
        }
        p = self.settings.data_dir / "heartbeat.json"
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(p)

    @tasks.loop(seconds=30)
    async def heartbeat(self):
        ok = await self.db.ping()
        self.module_status["database"] = "ok" if ok else "error"
        self._write_heartbeat()
        safe = self.safe_mode()
        if self._last_safe is not None and safe != self._last_safe and self.is_ready() and self.guild:
            log.warning("SAFE MODE %s", "ENABLED" if safe else "disabled")
            await self.db.add_event(type="safe_mode_on" if safe else "safe_mode_off", category="security",
                                    guild_id=self.guild.id, actor_confidence="unknown",
                                    details={"source": "file/env/config"}, source="bot")
            try:
                await self.refresh_presence()
            except Exception:  # noqa: BLE001
                pass
        self._last_safe = safe

    @tasks.loop(hours=1)
    async def maintenance(self):
        g = self.guild
        if not g:
            return
        r = self.cfg.retention
        try:
            n = await self.db.prune(r.events_days, r.message_events_days, r.voice_events_days)
            if n:
                log.info("retention pruned %d events", n)
            last = await self.db.latest_snapshot(g.id)
            due = last is None or (datetime.now(timezone.utc) - datetime.fromisoformat(last["ts"])).total_seconds() \
                >= r.auto_snapshot_hours * 3600
            if due:
                sid = await self.take_snapshot("auto", label="scheduled")
                log.info("auto snapshot #%d", sid)
            await self.db.prune_snapshots(g.id, r.keep_snapshots)
        except Exception:  # noqa: BLE001
            log.exception("maintenance failed")


def require_level(minimum: Level | str):
    """app_commands check using ServerBot's owner/admin/mod/baseline model."""
    lvl = Level.parse(minimum) if isinstance(minimum, str) else minimum

    async def predicate(interaction: discord.Interaction) -> bool:
        bot: ServerBot = interaction.client  # type: ignore[assignment]
        have = bot.level_of(interaction.user)
        if have < lvl:
            raise app_commands.CheckFailure(f"This needs **{lvl.name.title()}** access (you are {have.name.title()}).")
        return True

    return app_commands.check(predicate)


def perm_choices(current: str) -> list[app_commands.Choice[str]]:
    cur = current.lower().replace(" ", "_")
    names = [n for n in F.FLAGS if cur in n][:25]
    return [app_commands.Choice(name=F.pretty(n), value=n) for n in names]
