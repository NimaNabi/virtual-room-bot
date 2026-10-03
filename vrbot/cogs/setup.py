"""/setup — owner-only setup wizard.

Pages: 1 Identity → 2 Trust levels → 3 Layout → 4 Modules → 5 Preview → Apply.
Apply = snapshot → create/reuse → write this server's IDs into the runtime config → verify → baseline.
Idempotent: everything it creates is remembered (kv setup:obj:*), re-running reuses it, and it never deletes, renames
or moves anything that already exists. Giving existing members the default level is a separate, previewed step.
Security identity of a level is its key (trust_level_1..3); names and colours are display only.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time

import discord
from discord import app_commands
from discord.ext import commands

from ..config import save_server_config
from ..db import now_iso
from ..trust import ELEVATIONS, MOD_PERMS, tier_permissions, tier_role_ids, tiers_held
from ..ui import COLORS

log = logging.getLogger("vrbot.setup")
TIERS = ("tier1", "tier2", "tier3")
KEY = {"tier1": "trust_level_1", "tier2": "trust_level_2", "tier3": "trust_level_3"}
HEX = re.compile(r"^#?[0-9a-fA-F]{6}$")

DEFAULT_NAMES = {  # generic defaults; every one can be overridden in config `layout.names`
    "cat_info": "Info", "cat_community": "General", "cat_voice": "Voice", "cat_private": "Private", "cat_owner": "Owner",
    "welcome": "welcome", "rules": "rules", "menu": "control-center", "chat": "chat", "music": "music", "lobby": "Lobby",
    "hub": "➕ Create Room", "room_tier1": "{level} room", "room_tier2": "{level} room", "room_tier3": "{level} room",
    "owner_room": "Owner room", "alerts": "bot-alerts", "voice_log": "voice-activity", "server_log": "server-activity",
}
CATEGORIES = {"cat_info": ("recommended",), "cat_community": ("recommended",), "cat_voice": ("minimal", "recommended"),
              "cat_private": ("recommended",), "cat_owner": ("minimal", "recommended")}
CHANNELS = {  # key -> (kind, parent, plans)
    "welcome": ("text", "cat_info", ("recommended",)), "rules": ("text", "cat_info", ("recommended",)),
    "menu": ("text", "cat_info", ("recommended",)), "menu_min": ("text", "cat_voice", ("minimal",)),
    "chat": ("text", "cat_community", ("recommended",)), "music": ("text", "cat_community", ("recommended",)),
    "lobby": ("voice", "cat_voice", ("recommended",)), "hub": ("voice", "cat_voice", ("minimal", "recommended")),
    "room_tier1": ("voice", "cat_private", ("recommended",)), "room_tier2": ("voice", "cat_private", ("recommended",)),
    "room_tier3": ("voice", "cat_private", ("recommended",)), "owner_room": ("voice", "cat_owner", ("recommended",)),
    "alerts": ("text", "cat_owner", ("minimal", "recommended")), "voice_log": ("text", "cat_owner", ("minimal", "recommended")),
    "server_log": ("text", "cat_owner", ("minimal", "recommended")),
}
READ_ONLY = {"welcome", "rules", "menu", "menu_min"}
ROOM_ACCESS = {"room_tier1": ("tier1",), "room_tier2": ("tier1", "tier2"), "room_tier3": ("tier1", "tier2", "tier3")}
BOT_NEEDS = ["manage_roles", "manage_channels", "manage_guild", "view_audit_log", "move_members", "moderate_members",
             "view_channel", "send_messages", "embed_links", "connect", "speak"]
MODULES = {"music": "🎵 Music", "rooms": "🔊 Temporary rooms", "guests": "🎟️ Guest invites", "guardian": "🛡️ Guardian"}


def plan_items(plan: str) -> tuple[dict, dict]:
    plan = "minimal" if plan == "existing" else plan
    return ({k: v for k, v in CATEGORIES.items() if plan in v},
            {k: v for k, v in CHANNELS.items() if plan in v[2]})


def channel_name(key: str, overrides: dict, level_names: dict) -> str:
    name = (overrides or {}).get(key) or DEFAULT_NAMES[key.replace("menu_min", "menu")]
    if key.startswith("room_"):
        name = name.replace("{level}", level_names[KEY[key[5:]]])
    return name[:100]


def parse_color(text: str | None) -> int:
    t = (text or "").strip()
    if not t:
        return 0
    if not HEX.match(t):
        raise ValueError(f"'{t}' is not a colour like #4A90D9")
    return int(t.lstrip("#"), 16)


class Session:
    def __init__(self, cfg):
        self.name = cfg.identity.control_center_name
        self.subtitle = cfg.identity.subtitle
        self.nickname = cfg.identity.bot_nickname
        self.names = dict(cfg.trust.names)
        self.colors = dict(cfg.trust.colors)
        self.mapped: dict[str, int] = {}
        self.plan = "recommended"
        self.modules = {"music": cfg.music.enabled, "rooms": cfg.voice_rooms.enabled, "guests": True,
                        "guardian": cfg.guardian.enabled}


class IdentityModal(discord.ui.Modal, title="Identity"):
    def __init__(self, cog, sess):
        super().__init__()
        self.cog, self.sess = cog, sess
        self.n = discord.ui.TextInput(label="Menu name", default=sess.name, max_length=40)
        self.s = discord.ui.TextInput(label="Subtitle (optional)", default=sess.subtitle, required=False, max_length=80)
        self.k = discord.ui.TextInput(label="Bot nickname in this server (optional)", default=sess.nickname,
                                      required=False, max_length=32)
        for x in (self.n, self.s, self.k):
            self.add_item(x)

    async def on_submit(self, i: discord.Interaction):
        self.sess.name, self.sess.subtitle, self.sess.nickname = str(self.n).strip(), str(self.s).strip(), str(self.k).strip()
        await self.cog.page(i, 1)


class TrustModal(discord.ui.Modal, title="Trust level names and colours"):
    def __init__(self, cog, sess):
        super().__init__()
        self.cog, self.sess = cog, sess
        self.f = [discord.ui.TextInput(label=f"Level {n} name" + (" (highest)" if n == 1 else " (default)" if n == 3 else ""),
                                       default=sess.names[KEY[t]], max_length=32) for n, t in enumerate(TIERS, 1)]
        self.c = discord.ui.TextInput(label="Colours (optional): #hex, #hex, #hex", required=False, max_length=30,
                                      default=", ".join(sess.colors.get(KEY[t], "") for t in TIERS).strip(", "),
                                      placeholder="#4A90D9, #50B37A, #9AA0A6")
        for x in (*self.f, self.c):
            self.add_item(x)

    async def on_submit(self, i: discord.Interaction):
        names = [str(x).strip() for x in self.f]
        if len({n.lower() for n in names}) < 3:
            await i.response.send_message("The three names must be different.", ephemeral=True)
            return
        cols = [c.strip() for c in str(self.c).split(",")] if str(self.c).strip() else []
        try:
            [parse_color(c) for c in cols]
        except ValueError as e:
            await i.response.send_message(str(e), ephemeral=True)
            return
        for n, t in enumerate(TIERS):
            self.sess.names[KEY[t]] = names[n]
            if n < len(cols) and cols[n]:
                self.sess.colors[KEY[t]] = "#" + cols[n].lstrip("#").upper()
        await self.cog.page(i, 2)


class Setup(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.sessions: dict[int, Session] = {}
        self.lock = asyncio.Lock()

    # ---------------------------------------------------------- helpers
    @property
    def g(self) -> discord.Guild:
        return self.bot.guild

    def allowed(self, user) -> bool:
        g = self.g
        return bool(g) and (user.id == g.owner_id or user.id in self.bot.bot_owner_ids())

    async def configured(self) -> dict | None:
        return await self.bot.db.kv_get("setup:done")

    async def obj(self, key: str):
        oid = await self.bot.db.kv_get(f"setup:obj:{key}")
        if not oid:
            return None
        return self.g.get_role(int(oid)) if key.startswith(("role_", "elev_")) else self.g.get_channel(int(oid))

    def missing_bot_perms(self) -> list[str]:
        p = self.g.me.guild_permissions
        return [n for n in BOT_NEEDS if not getattr(p, n, False)]

    def sess(self, uid) -> Session:
        if uid not in self.sessions:
            self.sessions[uid] = Session(self.bot.cfg)
        return self.sessions[uid]

    def _btn(self, label, cb, style=discord.ButtonStyle.secondary, row=0, emoji=None, disabled=False):
        b = discord.ui.Button(label=label, style=style, row=row, emoji=emoji, disabled=disabled)

        async def wrapped(i: discord.Interaction):
            if not self.allowed(i.user):
                await i.response.send_message("Only the server owner can do that.", ephemeral=True)
                return
            await cb(i)
        b.callback = wrapped
        return b

    async def show(self, i: discord.Interaction, emb, view):
        if i.response.is_done():
            await i.edit_original_response(embed=emb, view=view)
        else:
            await i.response.edit_message(embed=emb, view=view)

    # ---------------------------------------------------------- fresh-install notice (owner only, once)
    @commands.Cog.listener()
    async def on_ready(self):
        g = self.g
        if not g or await self.configured():
            return
        log.warning("Not configured for %s yet — the server owner should run /setup", g.name)
        if await self.bot.db.kv_get("setup:notified"):
            return
        owner = g.owner or await self.bot.fetch_user(g.owner_id)
        try:
            await owner.send(f"👋 I'm in **{g.name}** but not set up yet. Run **/setup** in the server (only you can use it).")
        except discord.HTTPException:
            pass
        await self.bot.db.kv_set("setup:notified", True)

    # ---------------------------------------------------------- pages
    async def page(self, i: discord.Interaction, n: int):
        s = self.sess(i.user.id)
        v = discord.ui.View(timeout=1800)
        nav_next = lambda to: self._btn("Next", lambda x: self.page(x, to), discord.ButtonStyle.primary, row=4, emoji="▶️")  # noqa: E731
        nav_back = lambda to: self._btn("Back", lambda x: self.page(x, to), row=4, emoji="◀️")  # noqa: E731
        if n == 1:
            emb = discord.Embed(title="Setup · 1/5 · Identity", color=COLORS["BRAND"], description=(
                f"**Menu name:** {s.name}\n**Subtitle:** {s.subtitle or '—'}\n**Bot nickname here:** {s.nickname or '(unchanged)'}\n\n"
                "The bot's global username and avatar are set in the Discord Developer Portal (Bot page)."))
            v.add_item(self._btn("Edit", lambda x: x.response.send_modal(IdentityModal(self, s)), emoji="✏️"))
            v.add_item(nav_next(2))
        elif n == 2:
            lines = []
            for num, t in enumerate(TIERS, 1):
                k = KEY[t]
                mapped = self.g.get_role(s.mapped[t]).name if t in s.mapped else None
                lines.append(f"**Level {num}** — " + (f"existing role **{mapped}**" if mapped else
                             f"**{s.names[k]}** · colour {s.colors.get(k) or 'default'}"))
            emb = discord.Embed(title="Setup · 2/5 · Trust levels", color=COLORS["BRAND"], description=(
                "Level 1 has the highest normal trust, Level 3 is the default for everyone.\n\n" + "\n".join(lines)))
            v.add_item(self._btn("Customize names & colours", lambda x: x.response.send_modal(TrustModal(self, s)), emoji="✏️"))
            v.add_item(self._btn("Use existing roles…", lambda x: self.map_roles(x)))
            v.add_item(nav_back(1))
            v.add_item(nav_next(3))
        elif n == 3:
            emb = discord.Embed(title="Setup · 3/5 · Layout", color=COLORS["BRAND"], description=(
                f"**Selected:** {s.plan.title()}\n\n"
                "**Minimal** — only what the features need (menu, ➕ Create Room, owner-only logs).\n"
                "**Recommended** — a clean structure: info, community, voice, one private room per level, owner area.\n"
                "**Existing server** — Minimal, reusing your roles (map them on page 2)."))
            for label, plan in (("Minimal", "minimal"), ("Recommended", "recommended"), ("Existing server", "existing")):
                v.add_item(self._btn(label, lambda x, plan=plan: self._set(x, "plan", plan, 3),
                                     discord.ButtonStyle.success if s.plan == plan else discord.ButtonStyle.secondary))
            v.add_item(nav_back(2))
            v.add_item(nav_next(4))
        elif n == 4:
            emb = discord.Embed(title="Setup · 4/5 · Modules", color=COLORS["BRAND"], description=(
                "Tap to switch a module on or off.\n\n" + "\n".join(f"{'✅' if s.modules[k] else '⬜'} {label}" for k, label in MODULES.items())
                + "\n\n🤖 The AI assistant is optional and configured in `.env` (AI_ENABLED, AI_BASE_URL, AI_API_KEY, AI_MODEL)."))
            for k, label in MODULES.items():
                v.add_item(self._btn(label, lambda x, k=k: self._toggle(x, k),
                                     discord.ButtonStyle.success if s.modules[k] else discord.ButtonStyle.secondary))
            v.add_item(nav_back(3))
            v.add_item(nav_next(5))
        else:
            emb, missing = await self.preview(s)
            v.add_item(self._btn("Apply", self._apply, discord.ButtonStyle.success, row=4, disabled=bool(missing)))
            v.add_item(nav_back(4))
        await self.show(i, emb, v)

    async def _set(self, i, attr, value, page):
        setattr(self.sess(i.user.id), attr, value)
        await self.page(i, page)

    async def _toggle(self, i, key):
        s = self.sess(i.user.id)
        s.modules[key] = not s.modules[key]
        await self.page(i, 4)

    async def map_roles(self, i: discord.Interaction):
        s = self.sess(i.user.id)
        v = discord.ui.View(timeout=1800)
        for n, t in enumerate(TIERS):
            sel = discord.ui.RoleSelect(placeholder=f"Existing role for Level {n + 1} (empty = create new)", row=n,
                                        min_values=0, max_values=1)

            async def cb(si: discord.Interaction, t=t, sel=sel):
                role = sel.values[0] if sel.values else None
                if role and (role.managed or role >= self.g.me.top_role or role.permissions.administrator):
                    await si.response.send_message("That role can't be used (bot-managed, above the bot, or Administrator).",
                                                   ephemeral=True)
                    return
                if role:
                    s.mapped[t] = role.id
                    s.names[KEY[t]] = role.name
                else:
                    s.mapped.pop(t, None)
                await si.response.defer()
            sel.callback = cb
            v.add_item(sel)
        v.add_item(self._btn("Done", lambda x: self.page(x, 2), discord.ButtonStyle.primary, row=3))
        await self.show(i, discord.Embed(title="Use existing roles", color=COLORS["INFO"], description=(
            "Pick roles you already use. They're used as they are — names, colours and permissions are not changed.")), v)

    async def preview(self, s: Session):
        cats, chans = plan_items(s.plan)
        names = self.bot.cfg.layout.names
        lines = [f"**Menu:** {s.name}"]
        for n, t in enumerate(TIERS, 1):
            lines.append(f"{'♻️ use' if t in s.mapped else '✔️ keep' if await self.obj('role_' + t) else '➕ create'} role "
                         f"**{s.names[KEY[t]]}** (Level {n})")
        for kind, e in ELEVATIONS.items():
            lines.append(("✔️ keep" if await self.obj(f"elev_{kind}") else "➕ create") + f" role **{e['label']}** (temporary access)")
        for key in cats:
            lines.append(("✔️ keep" if await self.obj(key) else "➕ create") + f" category **{channel_name(key, names, s.names)}**")
        for key, (kind, parent, _) in chans.items():
            if key == "hub" and not s.modules["rooms"]:
                continue
            lines.append(("✔️ keep" if await self.obj(key) else "➕ create") + f" {'🔊' if kind == 'voice' else '#'} "
                         f"**{channel_name(key, names, s.names)}**")
        lines.append("**Modules:** " + ", ".join(label for k, label in MODULES.items() if s.modules[k]))
        emb = discord.Embed(title="Setup · 5/5 · Preview", color=COLORS["INFO"], description="\n".join(lines)[:3800])
        emb.add_field(name="Safe by design", inline=False, value=(
            "Nothing existing is deleted, renamed or moved. A snapshot is taken first. Members are not changed here."))
        missing = self.missing_bot_perms()
        if missing:
            emb.add_field(name="⛔ Can't apply yet", value="The bot is missing: " + ", ".join(missing), inline=False)
        return emb, missing

    # ---------------------------------------------------------- apply
    async def _apply(self, i: discord.Interaction):
        s = self.sess(i.user.id)
        await i.response.edit_message(embed=discord.Embed(description="⏳ Setting up…", color=COLORS["BRAND"]), view=None)
        async with self.lock:
            try:
                report = await self.apply(s, i.user)
            except Exception as e:  # noqa: BLE001
                log.exception("setup failed")
                await i.edit_original_response(embed=discord.Embed(
                    title="⚠️ Setup stopped", color=COLORS["ERROR"],
                    description=f"{type(e).__name__}: {e}\nNothing was deleted. Fix the cause and run /setup again."), view=None)
                return
        await i.edit_original_response(embed=discord.Embed(title="✅ Setup complete", color=COLORS["OK"],
                                                           description="\n".join(report)[:3900]), view=self.home_view(True))

    async def apply(self, s: Session, actor) -> list[str]:
        g, db, cfg = self.g, self.bot.db, self.bot.cfg
        self.bot.guard("setup", actor.id)
        snap = await self.bot.take_snapshot("pre-change", label="before setup", created_by=actor.id)
        reason = f"[bot: {actor} ({actor.id})] setup"
        from ..perms import flags as F
        perms = tier_permissions(cfg)
        created = []

        roles: dict[str, discord.Role] = {}
        for t in TIERS:
            r = g.get_role(s.mapped[t]) if t in s.mapped else await self.obj(f"role_{t}")
            if r is None:
                r = await g.create_role(name=s.names[KEY[t]], colour=discord.Colour(parse_color(s.colors.get(KEY[t]))),
                                        hoist=False, mentionable=False, permissions=discord.Permissions(F.value_of(perms[t])),
                                        reason=reason)
                created.append(f"role {r.name}")
            roles[t] = r
            await db.kv_set(f"setup:obj:role_{t}", r.id)
        elev = {}
        for kind, e in ELEVATIONS.items():
            r = await self.obj(f"elev_{kind}")
            if r is None:
                p = discord.Permissions(administrator=True) if kind == "admin" else discord.Permissions(F.value_of(MOD_PERMS))
                r = await g.create_role(name=e["label"], permissions=p, hoist=False, mentionable=False, reason=reason)
                created.append(f"role {r.name}")
            elev[kind] = r
            await db.kv_set(f"setup:obj:elev_{kind}", r.id)
        await db.kv_set("access:roles", {k: r.id for k, r in elev.items()})
        self.bot.elevation_role_ids = {r.id for r in elev.values()}

        cats_needed, chans_needed = plan_items(s.plan)
        if not s.modules["rooms"]:
            chans_needed.pop("hub", None)
        names, everyone = cfg.layout.names, g.default_role
        cats: dict[str, discord.CategoryChannel] = {}
        for key in cats_needed:
            c = await self.obj(key)
            if c is None:
                ow = {}
                if key in ("cat_owner", "cat_private"):
                    ow = {everyone: discord.PermissionOverwrite(view_channel=False),
                          g.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True, connect=True)}
                c = await g.create_category(channel_name(key, names, s.names), overwrites=ow, reason=reason)
                created.append(f"category {c.name}")
            cats[key] = c
            await db.kv_set(f"setup:obj:{key}", c.id)
        chans: dict[str, discord.abc.GuildChannel] = {}
        for key, (kind, parent, _) in chans_needed.items():
            ch = await self.obj(key)
            if ch is None:
                ow = dict(cats[parent].overwrites)
                if key in READ_ONLY:
                    ow[everyone] = discord.PermissionOverwrite(send_messages=False, add_reactions=True,
                                                               create_public_threads=False, create_private_threads=False)
                    ow[g.me] = discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True)
                for t in ROOM_ACCESS.get(key, ()):
                    ow[roles[t]] = discord.PermissionOverwrite(view_channel=True, connect=True, speak=True, stream=True)
                maker = g.create_voice_channel if kind == "voice" else g.create_text_channel
                ch = await maker(channel_name(key, names, s.names), category=cats[parent], overwrites=ow, reason=reason)
                created.append(f"channel {ch.name}")
            chans[key] = ch
            await db.kv_set(f"setup:obj:{key}", ch.id)
            await asyncio.sleep(0.3)

        # runtime configuration: THIS server's IDs + the owner's choices
        import yaml
        path = self.bot.settings.config_path
        data = (yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}) or {}
        data["identity"] = {"control_center_name": s.name, "subtitle": s.subtitle, "bot_nickname": s.nickname}
        tr = data.setdefault("trust", {})
        tr["names"] = {KEY[t]: roles[t].name for t in TIERS}
        tr["colors"] = {KEY[t]: s.colors[KEY[t]] for t in TIERS if s.colors.get(KEY[t])}
        b, p = data.setdefault("baseline", {}), data.setdefault("privacy", {})
        for n, t in enumerate(TIERS, 1):
            b.setdefault("trust_levels", {}).setdefault(KEY[t], {"rank": n})["role"] = str(roles[t].id)
            p.setdefault("tiers", {}).setdefault(KEY[t], {"rank": n})["roles"] = [str(roles[t].id)]
        if not p.get("owner_id"):
            p["owner_id"] = self.bot.settings.owner_id or g.owner_id
        areas = p.get("areas") or {}
        floors = {"cat_info": "public", "cat_community": "public", "cat_voice": "public", "cat_private": "trust_level_3",
                  "cat_owner": "owner_area", "room_tier1": "trust_level_1", "room_tier2": "trust_level_2",
                  "room_tier3": "trust_level_3"}
        for key, floor in floors.items():
            o = cats.get(key) or chans.get(key)
            if o is not None:
                areas[str(o.id)] = floor
        p["areas"] = areas
        ro_ids = {f"id:{chans[k].id}" for k in READ_ONLY if k in chans}
        rules = [r for r in (b.get("channels") or []) if r.get("match") not in ro_ids]
        rules += [{"match": m, "access": {"trust_level_1": "read", "trust_level_2": "read", "trust_level_3": "read",
                                          "default": "read"}} for m in sorted(ro_ids)]
        b["channels"] = rules
        b["allow_unsynced"] = sorted({*(b.get("allow_unsynced") or []),
                                      *[str(chans[k].id) for k in [*READ_ONLY, *ROOM_ACCESS] if k in chans]})
        vr = data.setdefault("voice_rooms", {})
        vr["enabled"] = s.modules["rooms"]
        if "hub" in chans:
            vr["create_channel"] = str(chans["hub"].id)
        data.setdefault("music", {})["enabled"] = s.modules["music"]
        gd = data.setdefault("guardian", {})
        gd["enabled"], gd["alert_channel"] = s.modules["guardian"], str(chans["alerts"].id)
        if not s.modules["guests"]:
            tr["guest_invite_levels"] = []
        save_server_config(path, data)
        self.bot.reload_config()

        kv = {"guardian:alert_channel_id": chans["alerts"].id, "ownerlogs:voice": chans["voice_log"].id,
              "ownerlogs:server": chans["server_log"].id, "channels:guide": (chans.get("menu") or chans.get("menu_min")).id}
        if "hub" in chans:
            kv["voicerooms:create_channel_id"] = chans["hub"].id
        for k in ("welcome", "rules", "chat", "music", "lobby"):
            if k in chans:
                kv[f"channels:{k}"] = chans[k].id
        for k, v in kv.items():
            await db.kv_set(k, v)
        if s.nickname:
            try:
                await g.me.edit(nick=s.nickname, reason=reason)
            except discord.HTTPException:
                created.append("(nickname could not be changed)")

        onb, app = self.bot.get_cog("Onboarding"), self.bot.get_cog("ControlCenter")
        published = 0
        if onb and app:
            published += "message" in await onb._upsert("guide", kv["channels:guide"], [app.public_embed()], app.public_view(), pin=True)
            ids = await onb.ids()
            if all(ids.get(k) for k in ("welcome", "chat", "lobby", "rules")):
                from .onboarding import HelpButtonView
                published += "message" in await onb._upsert("panel", ids["welcome"], [onb.welcome_embed(ids)],
                                                            HelpButtonView(self.bot, ids), pin=True)
                published += "message" in await onb._upsert("rules", ids["rules"], [onb.rules_embed()])

        await asyncio.sleep(2)
        live_roles = {int(r["id"]) for r in await self.bot.http.get_roles(g.id)}
        live_chans = {int(c["id"]) for c in await self.bot.http.get_all_guild_channels(g.id)}
        ok_roles = all(r.id in live_roles for r in [*roles.values(), *elev.values()])
        ok_chans = all(c.id in live_chans for c in [*cats.values(), *chans.values()])
        from ..perms.privacy import privacy_audit
        crit = [x.title for x in privacy_audit(self.bot.model(), self.bot.cfg, self.bot.user.id) if x.severity == "CRITICAL"]
        base = await self.bot.take_snapshot("manual", label="APPROVED BASELINE (setup)", created_by=actor.id)
        await db.kv_set("baseline:ts", now_iso())
        await db.kv_set("baseline:snapshot", base)
        await db.kv_set("setup:done", {"plan": s.plan, "ts": time.time(), "by": actor.id})
        return [f"**Layout:** {s.plan.title()} · snapshot before changes #{snap} · baseline #{base}",
                f"**Created:** {len(created)} — " + (", ".join(created) or "nothing new"),
                f"**Verified in Discord:** roles {'✅' if ok_roles else '⚠️'} · channels {'✅' if ok_chans else '⚠️'}",
                f"**Privacy check:** {'✅ no critical findings' if not crit else '⚠️ ' + '; '.join(crit[:3])}",
                f"**Menu published:** {published} message(s)", "",
                "**Next:** tap **Give default level…** so existing members get the default level (new members get it "
                "automatically afterwards). Raise people with /tier manage or the 🔐 Owner menu."]

    # ---------------------------------------------------------- default level for existing members
    def _untiered(self):
        roles = tier_role_ids(self.bot.cfg)
        return [m for m in self.g.members if not m.bot and not self.bot.cfg.privacy.is_owner_like(m.id, self.g.owner_id)
                and not tiers_held([r.id for r in m.roles], roles)]

    async def _default_level_preview(self, i: discord.Interaction):
        n = len(self._untiered())
        default = self.bot.cfg.trust.names.get("trust_level_3", "Level 3")
        v = discord.ui.View(timeout=600)
        v.add_item(self._btn(f"Give {n} member(s) {default}", self._default_level_apply, discord.ButtonStyle.success,
                             disabled=n == 0))
        v.add_item(self._btn("Back", self._home, emoji="◀️"))
        await self.show(i, discord.Embed(title="Give everyone the default level", color=COLORS["INFO"], description=(
            f"**{n}** member(s) have no trust level. They'll get **{default}**. Nobody loses any role.")), v)

    async def _default_level_apply(self, i: discord.Interaction):
        await i.response.edit_message(embed=discord.Embed(description="⏳ Assigning…", color=COLORS["BRAND"]), view=None)
        tier = self.bot.get_cog("Tier")
        await self.bot.take_snapshot("pre-change", label="before default level for existing members", created_by=i.user.id)
        done = failed = 0
        for m in self._untiered():
            res = await tier.set_tier(m, "tier3", i.user, "setup: default level")
            done += 1
            failed += "verified" not in res
            await asyncio.sleep(0.4)
        await self.bot.db.kv_set("tier:migrated", True)
        await i.edit_original_response(embed=discord.Embed(title="✅ Default level assigned", color=COLORS["OK"], description=(
            f"{done - failed} member(s) updated" + (f"; {failed} not verified" if failed else "") +
            ". New members get the default level automatically.")), view=self.home_view(True))

    # ---------------------------------------------------------- entry
    async def status_embed(self) -> discord.Embed:
        done = await self.configured()
        if not done:
            emb = discord.Embed(title="🛠️ Setup", color=COLORS["BRAND"], description=(
                "This server isn't set up yet. Five short pages: identity, trust levels, layout, modules, preview.\n"
                "Nothing that already exists is deleted, renamed or moved."))
        else:
            emb = discord.Embed(title="🛠️ Setup", color=COLORS["OK"], description=(
                f"Set up (**{done['plan'].title()}**) on <t:{int(done['ts'])}:f>. Running setup again only adds what's missing."))
        missing = self.missing_bot_perms()
        if missing:
            emb.add_field(name="⚠️ The bot is missing permissions", value=", ".join(missing), inline=False)
        return emb

    def home_view(self, done: bool) -> discord.ui.View:
        v = discord.ui.View(timeout=1800)
        v.add_item(self._btn("Start setup" if not done else "Run setup again", lambda x: self.page(x, 1),
                             discord.ButtonStyle.success))
        if done:
            v.add_item(self._btn("Give default level…", self._default_level_preview, discord.ButtonStyle.primary))
        return v

    async def _home(self, i: discord.Interaction):
        await self.show(i, await self.status_embed(), self.home_view(bool(await self.configured())))

    @app_commands.command(name="setup", description="Set up the bot in this server (owner only)")
    @app_commands.default_permissions(administrator=True)
    async def setup_cmd(self, interaction: discord.Interaction):
        if not self.allowed(interaction.user):
            await interaction.response.send_message("Only the server owner can run setup.", ephemeral=True)
            return
        await interaction.response.send_message(embed=await self.status_embed(),
                                                view=self.home_view(bool(await self.configured())), ephemeral=True)


async def setup(bot):
    await bot.add_cog(Setup(bot))
