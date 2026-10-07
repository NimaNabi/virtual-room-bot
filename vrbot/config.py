"""Configuration: secrets/ops from environment, server design from YAML."""
from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator

from .perms import flags as F


# ---------------------------------------------------------------- YAML model
class TrustLevelCfg(BaseModel):
    role: str
    rank: int
    description: str = ""
    require: list[str] = Field(default_factory=list)
    forbid: list[str] = Field(default_factory=list)
    allow_dangerous: list[str] = Field(default_factory=list)

    @field_validator("require", "forbid", "allow_dangerous")
    @classmethod
    def _perms(cls, v):
        return [F.normalize(p) for p in v]


class DefaultCfg(BaseModel):
    forbid: list[str] = Field(default_factory=lambda: sorted(F.DANGEROUS))
    require: list[str] = Field(default_factory=list)   # basics every member should have (@everyone)

    @field_validator("forbid", "require")
    @classmethod
    def _perms(cls, v):
        return [F.normalize(p) for p in v]


class ChannelRule(BaseModel):
    match: str  # "#general", "category:Gaming", "id:1234"
    access: dict[str, str | dict[str, bool]]

    def expectations(self, key: str) -> dict[str, bool] | None:
        a = self.access.get(key)
        if a is None:
            return None
        if isinstance(a, str):
            if a not in F.LEVELS:
                raise ValueError(f"Unknown access level {a!r}; use one of {sorted(F.LEVELS)}")
            return dict(F.LEVELS[a])
        return {F.normalize(k): v for k, v in a.items()}


class BaselineCfg(BaseModel):
    trust_levels: dict[str, TrustLevelCfg] = Field(default_factory=dict)
    default: DefaultCfg = Field(default_factory=DefaultCfg)
    channels: list[ChannelRule] = Field(default_factory=list)
    hierarchy: list[str] = Field(default_factory=list)  # role names, highest first
    allow_unsynced: list[str] = Field(default_factory=list)
    allow_member_overwrites: bool = False
    enforce_trust_level_order: bool = True     # False when role order must not advertise social rank


class TierCfg(BaseModel):
    rank: int                          # 0 = most private (owner only) … highest = public
    roles: list[str] = Field(default_factory=list)   # role IDs granting this tier
    label: str = ""


class PrivacyCfg(BaseModel):
    """Trust/privacy hierarchy ("floors"). A member's tier is the MOST private tier among their roles;
    they may access every area whose tier rank is >= theirs. The canonical server owner sees everything.
    Areas map channel/category IDs to tier keys (a category covers its channels unless a channel has its own entry)."""
    owner_id: int | None = None
    approved_owner_equivalents: list[int] = Field(default_factory=list)  # explicit user IDs only (never inferred)
    tiers: dict[str, TierCfg] = Field(default_factory=dict)
    areas: dict[str, str] = Field(default_factory=dict)
    voice_powers_forbidden: bool = True   # Move/Mute/Deafen on non-owner roles leak voice presence (see docs)
    social_privacy: bool = True           # tier roles must not reveal rank (names, hoist, colours, mentions)

    def is_owner_like(self, uid: int, guild_owner_id: int | None = None) -> bool:
        return uid in {self.owner_id, guild_owner_id, *self.approved_owner_equivalents} - {None}

    def tier_rank(self, key: str) -> int:
        return self.tiers[key].rank

    @property
    def public_key(self) -> str | None:
        return max(self.tiers, key=lambda k: self.tiers[k].rank) if self.tiers else None


class AccessCfg(BaseModel):
    owner_ids: list[int] = Field(default_factory=list)
    admin_roles: list[str] = Field(default_factory=list)
    moderator_roles: list[str] = Field(default_factory=list)
    repair_min_level: str = "owner"   # owner | admin
    ai_min_level: str = "mod"


class WelcomeCfg(BaseModel):
    enabled: bool = False
    channel: str | None = None
    message: str = "Welcome {mention} to **{server}**! 👋"
    leave_enabled: bool = False
    leave_message: str = "**{name}** left the server."
    dm_enabled: bool = False
    dm_message: str = "Welcome to {server}!"
    default_roles: list[str] = Field(default_factory=list)


class SecurityCfg(BaseModel):
    bot_admin_intended: bool = True     # Administrator on the bot is deliberate
    safe_mode: bool = False             # READ-ONLY kill switch (also: SAFE_MODE=true env or data/SAFE_MODE file)
    max_mod_actions_per_10min: int = 5  # per actor, kicks+bans+timeouts
    max_changes_per_batch: int = 40     # larger permission batches are refused (split by trust_level)
    min_account_age_days: int = 7
    raid_joins: int = 6
    raid_window_seconds: int = 60


class RetentionCfg(BaseModel):
    events_days: int = 365
    message_events_days: int = 30
    voice_events_days: int = 180
    keep_snapshots: int = 60
    auto_snapshot_hours: int = 24


class MessageLogCfg(BaseModel):
    """Message edit/delete logging is OPTIONAL and off unless the server config turns it on (privacy)."""
    metadata: bool = False  # deletes/edits: who/where/when (no content)
    content: bool = False   # also keep the text; requires the privileged MESSAGE_CONTENT intent
    content_days: int = Field(7, ge=1)  # message text is erased from the log after this many days


class AutoHealCfg(BaseModel):
    enabled: bool = False
    interval_minutes: int = 60
    max_changes: int = 3
    trust_levels_only: bool = True


class MusicCfg(BaseModel):
    enabled: bool = True
    default_volume: int = 60
    max_queue: int = 200
    idle_disconnect_seconds: int = 300
    search_prefix: str = "ytsearch"
    idle_247_minutes: int = 10      # 24/7 radio sleeps after this long with nobody in the channel


class GuardianCfg(BaseModel):
    enabled: bool = True
    alert_channel: str | None = None       # falls back to log_channel; /guardian setup-channel sets one
    post_min_severity: str = "WARNING"     # CRITICAL | WARNING | INFO posted to the alert channel
    dm_owner_on_critical: bool = True
    trusted_bots: list[str] = Field(default_factory=list)  # bot user IDs/names whose addition is not CRITICAL
    drift_check_minutes: int = 10
    dedupe_minutes: int = 30
    mass_action_count: int = 3             # bans/kicks/channel deletions by one actor...
    mass_action_window_seconds: int = 300  # ...within this window => CRITICAL
    failed_command_count: int = 3          # denied admin/mod commands by one user in 10 min => WARNING


class SummaryCfg(BaseModel):
    daily: bool = False
    weekly: bool = False
    weekday: int = 6                       # 0=Monday … 6=Sunday
    hour_utc: int = 18
    channel: str | None = None             # falls back to guardian alert channel


class VoiceRoomsCfg(BaseModel):
    enabled: bool = False
    create_channel: str | None = None      # "➕ Create Room" voice channel (name or id)
    category: str | None = None            # where rooms are created (default: the create channel's category)
    name_template: str = "{name}'s Room"
    default_limit: int = 0
    max_rooms: int = 15


class AICfg(BaseModel):
    enabled: bool = True
    max_tool_rounds: int = 6


class SystemCfg(BaseModel):
    backups_enabled: bool = True       # scheduled local database backups (never sent anywhere)
    backup_hour: int = Field(4, ge=0, le=23)   # host-local time (set TZ for the container)
    backup_keep: int = Field(7, ge=1, le=60)
    downtime_min_minutes: int = Field(3, ge=1)  # shorter gaps (quick restarts, reconnects) are not reported
    downtime_dm_owner: bool = False    # optional DM in addition to the owner log


class IdentityCfg(BaseModel):
    control_center_name: str = "Control Center"   # title of the 🎛️ menu
    subtitle: str = ""                             # optional line under the title
    bot_nickname: str = ""                         # the bot's nickname in this server ("" = leave unchanged)


class TrustCfg(BaseModel):
    """Presentation + preferences of the three trust levels. Security identity is the KEY (trust_level_1..3);
    names/colours are display only and never change authorization."""
    names: dict[str, str] = Field(default_factory=lambda: {"trust_level_1": "Trusted", "trust_level_2": "Standard",
                                                           "trust_level_3": "Member"})
    colors: dict[str, str] = Field(default_factory=dict)          # "#RRGGBB"; empty = Discord's default colour
    permissions: dict[str, list[str]] = Field(default_factory=dict)  # extra permissions per level (added to the preset)
    guest_invite_levels: list[str] = Field(default_factory=lambda: ["trust_level_1"])


class LayoutCfg(BaseModel):
    names: dict[str, str] = Field(default_factory=dict)   # channel/category names used by /setup (overrides)


class OnboardingCfg(BaseModel):
    rules: list[list[str]] = Field(default_factory=lambda: [
        ["🤝", "Be respectful", "No harassment, hate speech or personal attacks."],
        ["📣", "No spam", "No mass mentions, unsolicited advertising or invite links to other servers."],
        ["🔞", "Keep it appropriate", "No NSFW or illegal content."],
        ["📜", "Follow Discord's rules", "Discord's Terms of Service and Community Guidelines apply here."],
    ])


class ServerConfig(BaseModel):
    identity: IdentityCfg = Field(default_factory=IdentityCfg)
    trust: TrustCfg = Field(default_factory=TrustCfg)
    layout: LayoutCfg = Field(default_factory=LayoutCfg)
    onboarding: OnboardingCfg = Field(default_factory=OnboardingCfg)
    access: AccessCfg = Field(default_factory=AccessCfg)
    baseline: BaselineCfg = Field(default_factory=BaselineCfg)
    log_channel: str | None = None
    post_events: list[str] = Field(default_factory=lambda: [
        "member_ban", "member_unban", "member_kick", "timeout_add", "voice_disconnect", "raid_alert",
        "account_age_warning", "overwrite_update", "overwrite_create", "overwrite_delete", "role_update",
        "mod_action", "repair_applied", "autoheal",
    ])
    welcome: WelcomeCfg = Field(default_factory=WelcomeCfg)
    security: SecurityCfg = Field(default_factory=SecurityCfg)
    retention: RetentionCfg = Field(default_factory=RetentionCfg)
    message_logging: MessageLogCfg = Field(default_factory=MessageLogCfg)
    autoheal: AutoHealCfg = Field(default_factory=AutoHealCfg)
    music: MusicCfg = Field(default_factory=MusicCfg)
    ai: AICfg = Field(default_factory=AICfg)
    privacy: PrivacyCfg = Field(default_factory=PrivacyCfg)
    guardian: GuardianCfg = Field(default_factory=GuardianCfg)
    summaries: SummaryCfg = Field(default_factory=SummaryCfg)
    voice_rooms: VoiceRoomsCfg = Field(default_factory=VoiceRoomsCfg)
    system: SystemCfg = Field(default_factory=SystemCfg)


DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config" / "default.yaml"


def ensure_runtime_config(path: str | Path) -> Path:
    """First start: copy the shipped template to the runtime location (kept with the persistent data)."""
    p = Path(path)
    if not p.exists() and DEFAULT_CONFIG.exists() and p != DEFAULT_CONFIG:
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(DEFAULT_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
        except OSError:
            return DEFAULT_CONFIG  # read-only location: run with the template defaults
    return p


def load_server_config(path: str | Path, settings: "Settings | None" = None) -> ServerConfig:
    p = ensure_runtime_config(path)
    data = (yaml.safe_load(p.read_text(encoding="utf-8")) or {}) if p.exists() else {}
    cfg = ServerConfig.model_validate(data)
    if settings is not None:  # environment overrides (OWNER_ID / OWNER_EQUIVALENT_IDS)
        if settings.owner_id:
            cfg.privacy.owner_id = settings.owner_id
        if settings.owner_equivalent_ids:
            cfg.privacy.approved_owner_equivalents = sorted(set(cfg.privacy.approved_owner_equivalents)
                                                            | set(settings.owner_equivalent_ids))
        if not settings.ai_enabled:
            cfg.ai.enabled = False
    return cfg


def save_server_config(path: str | Path, data: dict) -> None:
    Path(path).write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")


def _ids(raw: str | None) -> list[int]:
    return [int(x) for x in (raw or "").replace(" ", "").split(",") if x.strip().isdigit()]


# ----------------------------------------------------------------- env/ops
class Settings(BaseModel):
    token: str | None
    guild_id: int | None
    owner_ids: list[int]
    owner_id: int | None = None
    owner_equivalent_ids: list[int] = []
    ai_enabled: bool = False
    data_dir: Path
    config_path: Path
    log_level: str
    lavalink_uri: str | None
    lavalink_password: str | None
    ai_base_url: str | None
    ai_api_key: str | None
    ai_model: str
    ai_fallback_models: list[str]
    message_content_intent: bool
    presence_intent: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        e = os.environ.get
        ids = _ids(e("OWNER_IDS"))
        return cls(
            token=e("DISCORD_TOKEN") or None,
            guild_id=int(e("GUILD_ID")) if e("GUILD_ID") else None,
            owner_ids=ids,
            owner_id=int(e("OWNER_ID")) if (e("OWNER_ID") or "").strip().isdigit() else None,
            owner_equivalent_ids=_ids(e("OWNER_EQUIVALENT_IDS")),
            ai_enabled=(e("AI_ENABLED", "false").lower() == "true"),
            data_dir=Path(e("DATA_DIR", "/data")),
            config_path=Path(e("CONFIG_PATH", "/data/server.yaml")),
            log_level=e("LOG_LEVEL", "INFO"),
            lavalink_uri=e("LAVALINK_URI") or None,
            lavalink_password=e("LAVALINK_PASSWORD") or None,
            ai_base_url=e("AI_BASE_URL") or None,
            ai_api_key=e("AI_API_KEY") or None,
            ai_model=e("AI_MODEL", "default"),
            ai_fallback_models=[m for m in (e("AI_FALLBACK_MODELS") or "").split(",") if m],
            message_content_intent=(e("MESSAGE_CONTENT_INTENT", "false").lower() == "true"),
            presence_intent=(e("PRESENCE_INTENT", "false").lower() == "true"),
        )
