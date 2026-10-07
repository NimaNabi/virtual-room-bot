# Virtual Room Bot

A self-hosted, configurable Discord bot with a tap-first menu (🎛️ Control Center) inside Discord.

- 🎵 **Music.** Quick Play, search, queue, player controls, internet radio and 24/7 mode, favourites, recent and popular tracks.
- 🔊 **Temporary voice rooms.** Join **➕ Create Room** to get your own room with a control panel (lock, hide, invite, trust, block, transfer).
- 🧭 **Trust levels.** Three levels decide who sees which private areas. You choose their names and colours, and each member holds exactly one level.
- 🎟️ **Guest invites.** Chosen levels can bring friends in with one-time invites. New members get the default level.
- 🔑 **Temporary access.** Temporary moderator or admin access always expires.
- 🛡️ **Owner tools.** Private logs (voice, joins, server changes) with *Find person* (works after renames and departures), a privacy and permission doctor, Guardian alerts, snapshots and optional auto-heal. Permission changes are shown in plain words ("✅ Timeout Members — now allowed").
- 🩺 **System health.** Owner → System shows uptime, the last downtime ("offline for 47 min, from → to, likely reason"), nightly local database backups with **Backup now**, and the state of every module.
- 🚫 **Optional AutoMod.** Blocked words (with `*` wildcards, Unicode- and Arabic-script-aware) and invite-link filtering, configured with buttons. Off by default.
- 🎮 **Social.** Tonight's plan and random teams.

Everything runs on **your** machine: the bot, its SQLite database and a local Lavalink music node. The bot only talks to Discord, the music or radio sources you play, and an AI API if you configure one.

## Before you start: where the bot runs
Virtual Room Bot is **self-hosted**: it runs on a computer you control (your PC, a home server or a VPS), not on
Discord's servers. While that computer is on and online, the bot is online. A PC is perfectly fine for trying it and
for everyday use; a VPS or an always-on machine is only needed if you want it online 24/7.

## Requirements
- **Docker Desktop** (Windows 10/11 or macOS) or Docker Engine with Compose (Linux). Free: https://www.docker.com/products/docker-desktop/
- **Git** (optional but recommended: it makes updates one command). https://git-scm.com/downloads
- A Discord account that owns the server you want to use.
- About 1 GB of free RAM.

## Quick start
```
git clone https://github.com/NimaNabi/virtual-room-bot.git
cd virtual-room-bot
```
(Or download the ZIP from the **Releases** page and extract it.) Then follow the steps below, or let an AI agent do it.

## Easiest install: with an AI coding agent
1. Open the folder in Claude Code, Codex or a similar coding agent.
2. Say: **"Set this Discord bot up for me using SETUP_WITH_AI.md."**
3. Do the few steps it asks of you: create the Discord application, provide the token, invite the bot, run `/setup`.

## Manual install (Docker)
1. **Create the bot.**
   - Go to https://discord.com/developers/applications → **New Application**.
   - Under **Bot**, turn on **Server Members Intent**.
   - Click **Reset Token** and copy the token.
2. **Configure.**
   - Run `bash scripts/init-env.sh`. On Windows without bash, copy `.env.example` to `.env` and set `LAVALINK_PASSWORD` to a long random string.
   - Put the token in `.env` as `DISCORD_TOKEN=…`, or run `bash scripts/set-token.sh`.
3. **Start.** Run `docker compose up -d --build`.
4. **Invite the bot.** Run `docker compose exec bot python -m vrbot.cli invite` to print the invite link, or use the link below with your Application ID:
   ```
   https://discord.com/oauth2/authorize?client_id=YOUR_APPLICATION_ID&scope=bot+applications.commands&permissions=1376838348023
   ```
   Then, in **Server Settings → Roles**, drag the bot's role above the roles it should manage.
5. **Set up.** In your server, run **/setup** (server owner only). It has five short pages:
   1. Identity: the menu name, an optional subtitle and the bot's nickname.
   2. Trust levels: keep the defaults, customize names and colours, or use roles you already have.
   3. Layout: Minimal, Recommended or Existing server.
   4. Modules: Music, Temporary rooms, Guest invites, Guardian.
   5. Preview, then Apply.

   Afterwards, tap **Give default level…** so existing members get the default level.
6. **Use it.** Open the 🎛️ menu channel and tap **Music**, **My Room** and so on.

## Configuration
- `.env` holds secrets and switches: the token, owner IDs, intents, music, and the optional AI. See `.env.example`.
- `/data/server.yaml`, inside the data volume, holds your server's configuration. It is created from `config/default.yaml` on first start and filled in by `/setup`. See `docs/CONFIGURATION.md`.
- The bot's global username and avatar are set in the Developer Portal. Its nickname in your server is set by `/setup`.
- AI is off by default. To enable it, set `AI_ENABLED=true` plus `AI_BASE_URL`, `AI_API_KEY` and `AI_MODEL` for any OpenAI-compatible API.

## Everyday commands
```
docker compose ps                 # status (both services should be healthy)
docker compose logs -f bot        # live logs
docker compose restart bot
docker compose down               # stop (data is kept)
```

## Updating
The bot tells the owner when a new version exists (🎛️ → Owner → **Updates**: installed and latest version, release notes). It never updates itself.

**Git installs (recommended):**
```
bash scripts/update.sh check      # see what's new
bash scripts/update.sh apply      # backup → new release → rebuild → health check → automatic rollback on failure
bash scripts/update.sh rollback   # back to the previous version (code + database backup)
```
Optional: keep `bash scripts/update.sh watch` running (e.g. in tmux or as a service) so the owner's **Update** button applies updates.
The repository is public, so checks and downloads need no account or token. For daily in-Discord notifications without the host check, set `UPDATE_REPO=NimaNabi/virtual-room-bot` in `.env`. Only stable releases (tags) are offered, never unreleased commits.

**ZIP installs:** get the new ZIP from the **Releases** page, extract it to a new folder, copy your `.env` into it, run `docker compose down` in the old folder and `docker compose up -d --build` in the new one. Keep the old folder until the new version runs (that's your rollback).

Your configuration and data live in the Docker volume `virtual-room-bot_vrbot-data`, never in the code folder, so updates keep them. Database migrations run automatically and only add.

## Backups and restore
The bot backs up its database **every night** (04:00 in your `TZ`, configurable under `system:` in `server.yaml`) and keeps the last 7. If the computer was off at that time, it catches up once when it's back. Backups stay on your machine; nothing is uploaded. Owner → System shows the last and next backup, and **Backup now**.
```
docker compose exec bot python -m vrbot.cli backups            # list backups
docker compose exec bot python -m vrbot.cli backup             # extra manual copy
```
**Restore** is deliberately not a button. Stop the bot, restore, start it again:
```
docker compose stop bot
docker compose run --rm bot python -m vrbot.cli restore auto-20261007-040000.db
docker compose up -d bot
```
It checks the backup's integrity, asks you to type RESTORE, and keeps your current database as a `pre-restore-…` copy.
To copy everything (database, configuration, backups) off the machine:
```
docker run --rm -v virtual-room-bot_vrbot-data:/data -v "$PWD":/out alpine tar czf /out/vrbot-data.tgz -C /data .
```

## Privacy defaults
- **Message edit/delete logging is off.** Turn it on in `server.yaml` (`message_logging.metadata: true`) to log who edited or deleted what, where and when. Message *text* is only kept with `content: true` plus the Message Content intent, and is erased after `content_days` (7). Log channels are never logged.
- **AutoMod is off** until the owner turns it on (Owner → System → AutoMod). It needs `MESSAGE_CONTENT_INTENT=true`.
- **Downtime notices** go to the owner-only log; a DM is optional (`system.downtime_dm_owner`).

## Troubleshooting
| Symptom | Check |
|---|---|
| Bot offline | `docker compose logs bot`. "DISCORD_TOKEN is not set" means the token is missing from `.env`. |
| Invalid token | Reset it in the Developer Portal and run `scripts/set-token.sh`. |
| Privileged intents error | Turn on **Server Members Intent**. Only set `MESSAGE_CONTENT_INTENT` / `PRESENCE_INTENT` to true if they're on in the portal too. |
| Music unavailable | `docker compose ps`: lavalink must be healthy. Check `COMPOSE_PROFILES=music` and `LAVALINK_PASSWORD` in `.env`. |
| `/setup` reports missing permissions | Re-invite with the link above, or give the bot's role those permissions and move it higher. |
| Configuration errors | `docker compose exec bot python -m vrbot.cli check-config` |

## Removing the bot
1. Kick the bot from your server.
2. Run `docker compose down -v`. This deletes the data volume, so back it up first if you want to keep it.
3. Optionally delete what `/setup` created; its report lists the roles and channels. The bot never deletes your messages.

## Security and privacy
- Your data (database, logs, backups, configuration) stays on your machine, in the Docker volume.
- The bot contacts only Discord, the music/radio sources you play, GitHub's public API if you set `UPDATE_REPO`,
  and an AI API if you configure one. No telemetry.
- Secrets (bot token, `LAVALINK_PASSWORD`, AI keys) live only in `.env`, which is never committed.
- Report vulnerabilities privately: see [SECURITY.md](SECURITY.md).

## For developers
[ARCHITECTURE.md](ARCHITECTURE.md) explains the code layout; [CONTRIBUTING.md](CONTRIBUTING.md) explains local
development, tests and pull requests.

## License
[MIT](LICENSE): use, modify and share it freely. Third-party components: `docs/THIRD_PARTY.md`. See also `NOTICE.md`.
