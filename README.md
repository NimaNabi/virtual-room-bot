# Virtual Room Bot

A free, self-hosted Discord bot for friend groups and small communities. Everything is done with buttons in a private
**🎛️ Control Center** inside Discord: music, your own voice room, inviting friends, and owner tools that keep the
server private and safe.

> **Self-hosted** means it runs on a computer you control (your Windows PC, a home server or a VPS) — not on
> Discord's servers. While that computer is on and online, the bot is online.

## What it does
**For everyone**
- 🎛️ **Control Center** — a tap-first, app-like menu (Back/Home, works on phones); only you see your menu.
- 🎵 **Music & radio** — Quick Play, search, queue, player controls, internet radio and 24/7 mode, favourites, recent and popular tracks.
- 🔊 **Your own voice room** — join **➕ Create Room**: lock/unlock, hide/show, rename, user limit, invite, trust/block, hand over ownership; empty rooms delete themselves.
- 🎟️ **Bring a friend** — one-time guest invites, sponsored by trusted members.
- 🎮 **Social** — Tonight's plan and random teams.

**For the owner**
- 🧭 **Trust levels & temporary access** — three levels decide who sees which private areas; temporary moderator/admin access always expires.
- 📜 **Structured logs** — voice, members, moderation and server changes in owner-only channels, with clickable Discord identities (no pings), **Find person** (by name or ID, works after renames and departures, shows roles at leave), readable permission changes (*✅ Timeout Members — now allowed*) and careful attribution (*actor unknown* instead of a guess).
- 🛡️ **Guardian, Privacy Doctor & Auto-Heal** — alerts on risky permission changes, a check that private areas really are private, structure snapshots and optional automatic repair.
- 🩺 **System** — version, uptime, downtime reports (*offline 47 min, from → to, likely reason*), nightly local database backups with **Backup now**, module health.
- 🔨 **Moderation** — kicks, bans, timeouts and warnings with a searchable history; optional **AutoMod** (blocked words, invite links); optional message edit/delete logging. Both off by default.
- ⬆️ **Safe updates** — the bot tells you about new releases; updating makes a backup, checks health and rolls back automatically if something is wrong.

Your data stays on your machine. The bot talks only to Discord, the music/radio sources you play, GitHub (update
checks, if enabled) and an AI API only if you configure one. No telemetry.

## Quick start — Windows (easiest)
Windows 10/11 (64-bit), ~1.5 GB disk. **No Docker, no Python or Java installs, no administrator rights.**

1. Download **`VirtualRoomBot-<version>.zip`** from the [Releases](https://github.com/NimaNabi/virtual-room-bot/releases/latest) page and extract it (e.g. to `C:\VirtualRoomBot`).
2. Double-click **`Setup.cmd`**. It downloads a private copy of Python, Java and the music server into that folder (each checked against a fixed checksum; nothing is installed system-wide).
3. Follow the prompts: create the bot in the Discord Developer Portal (it tells you exactly where to click), paste the bot token into the hidden prompt, and it opens the **invite link**.
4. Invite the bot to your server, then type **`/setup`** in Discord.

Afterwards use the **Virtual Room Bot** shortcut on your desktop: Start, Stop, Restart, Status, Logs, Backup, Updates,
Diagnostics, "Start with Windows". If the bot crashes it is restarted automatically (with a growing delay; it stops
trying after repeated crashes and the menu shows why). Uninstall: delete the folder and the two shortcuts.

*Automated fresh-install, lifecycle, crash-recovery and update/rollback tests pass on Windows. Real-world PCs and
Discord setups vary — if something doesn't work, please [open an issue](https://github.com/NimaNabi/virtual-room-bot/issues/new/choose).*
Windows may show a SmartScreen warning for downloaded scripts: choose **More info → Run anyway** only if you got the
ZIP from this repository's Releases page.

## Docker — Windows, macOS, Linux
Needs Docker Desktop (or Docker Engine with Compose) and ~1 GB RAM. Git is recommended (one-command updates).
```
git clone https://github.com/NimaNabi/virtual-room-bot.git
cd virtual-room-bot
bash scripts/init-env.sh          # creates .env (no bash? copy .env.example to .env, set a long random LAVALINK_PASSWORD)
bash scripts/set-token.sh         # paste the bot token (hidden, checked with Discord) — or put DISCORD_TOKEN= in .env
docker compose up -d --build
docker compose exec bot python -m vrbot.cli invite     # prints the invite link
```
Create the bot first: https://discord.com/developers/applications → **New Application** → **Bot**: turn on
**Server Members Intent**, **Reset Token**, copy it. After inviting, drag the bot's role above the roles it should
manage (Server Settings → Roles) and run **`/setup`**.

**Or let an AI agent do it:** open the folder in Claude Code, Codex or similar and say *"Set this Discord bot up for
me using SETUP_WITH_AI.md."* It covers both Windows and Docker and never needs your token in the chat.

## Server / VPS (24/7)
Same as Docker, on a Linux machine that stays on. Use a Git checkout so `scripts/update.sh` can update and roll back.
Docker restarts the bot if it crashes, and the bot's own watchdog makes it restart if it freezes or can't reconnect.

## `/setup` in Discord
Five short pages (server owner only): identity (menu name, nickname) → trust levels (keep defaults, rename/recolour,
or reuse existing roles) → layout (**Minimal**, **Recommended** or **Existing server**) → modules (Music, Temporary
rooms, Guest invites, Guardian) → preview → **Apply**, then **Give default level…** for existing members.

## Configuration
| What | Where |
|---|---|
| Token, owner IDs, intents, music on/off, AI, update checks, `TZ` | `.env` (see `.env.example`) |
| Your server's setup (roles, channels, modules, backups, logging) | `server.yaml` — Windows: `data\server.yaml`; Docker: inside the `vrbot-data` volume. Created by `/setup`; details in [docs/CONFIGURATION.md](docs/CONFIGURATION.md) |
| Bot name and avatar | Discord Developer Portal |

Optional features: **AI assistant** (`AI_ENABLED=true` + any OpenAI-compatible `AI_BASE_URL`, `AI_API_KEY`,
`AI_MODEL`), **update notifications** (`UPDATE_REPO=NimaNabi/virtual-room-bot`), **AutoMod** (Owner → System →
AutoMod; needs `MESSAGE_CONTENT_INTENT=true`), **message edit/delete logging** (`message_logging.metadata: true` in
`server.yaml`; message text is kept only with `content: true` and erased after 7 days).

## Updating
- **Windows:** desktop menu → **Check for updates**. Verified download (SHA-256) → backup → install → health check → automatic rollback.
- **Docker with Git:** `bash scripts/update.sh check`, then `bash scripts/update.sh apply` (same safety steps); `bash scripts/update.sh rollback` to go back.
- **Docker from a ZIP:** extract the new ZIP to a new folder, copy `.env` over, `docker compose down` in the old folder, `docker compose up -d --build` in the new one.

Only stable releases are offered. Data and configuration are never in the code folder's tracked files, so updates
keep them; database migrations are automatic and only add.

## Backups
Nightly at 04:00 (host time), the last 7 are kept, plus **Backup now** in Owner → System. Backups never leave your
machine. Restoring is deliberately not a button — see [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md#restore-a-backup).

## Problems?
| Symptom | First thing to check |
|---|---|
| Bot offline | Windows: menu → **Status** / **View logs**. Docker: `docker compose logs bot` |
| "Invalid token" | Reset the token in the Developer Portal; Windows: menu → **Change bot token**; Docker: `bash scripts/set-token.sh` |
| "Privileged intents" error | Developer Portal → Bot → turn on **Server Members Intent** (and Message Content only if you enabled AutoMod) |
| No music | Windows: **Status** must show *Music: running*; Docker: `lavalink` must be healthy in `docker compose ps` |
| Something else | **Diagnostics** (Windows menu, or `docker compose exec bot python -m vrbot.cli diagnostics`) and [open an issue](https://github.com/NimaNabi/virtual-room-bot/issues/new/choose) |

More: [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md). **Never post your bot token or API keys** — the diagnostics report leaves them out.

## Removing the bot
Kick it from your server. Windows: menu → Stop, then delete the folder and the two shortcuts. Docker:
`docker compose down -v` (deletes the data volume — back it up first). `/setup`'s report lists the roles and channels it
created; the bot never deletes your messages.

## Contributing, security, license
- [ARCHITECTURE.md](ARCHITECTURE.md) — code map · [CONTRIBUTING.md](CONTRIBUTING.md) — dev setup, tests, pull requests
- [SECURITY.md](SECURITY.md) — report vulnerabilities privately · [CHANGELOG.md](CHANGELOG.md) — what changed
- [MIT](LICENSE) — use, modify and share freely. Third-party components: [docs/THIRD_PARTY.md](docs/THIRD_PARTY.md), [NOTICE.md](NOTICE.md).
