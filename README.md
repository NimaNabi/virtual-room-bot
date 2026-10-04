# Virtual Room Bot

A self-hosted, configurable Discord bot with a tap-first menu (🎛️ Control Center) inside Discord.

- 🎵 **Music.** Quick Play, search, queue, player controls, internet radio and 24/7 mode, favourites, recent and popular tracks.
- 🔊 **Temporary voice rooms.** Join **➕ Create Room** to get your own room with a control panel (lock, hide, invite, trust, block, transfer).
- 🧭 **Trust levels.** Three levels decide who sees which private areas. You choose their names and colours, and each member holds exactly one level.
- 🎟️ **Guest invites.** Chosen levels can bring friends in with one-time invites. New members get the default level.
- 🔑 **Temporary access.** Temporary moderator or admin access always expires.
- 🛡️ **Owner tools.** Private logs (voice, joins, server changes), a privacy and permission doctor, Guardian alerts, snapshots and optional auto-heal.
- 🎮 **Social.** Tonight's plan and random teams.

Everything runs on **your** machine: the bot, its SQLite database and a local Lavalink music node. The bot only talks to Discord, the music or radio sources you play, and an AI API if you configure one.

## Requirements
- Docker with Docker Compose (Docker Desktop on Windows/macOS, or Docker Engine on Linux).
- A Discord account that owns the server you want to use.
- About 1 GB of free RAM.

## Easiest install: with an AI coding agent
1. Clone this repository or extract the ZIP.
2. Open the folder in Claude Code, Codex or a similar coding agent.
3. Say: **"Read SETUP_WITH_AI.md and configure this bot for my Discord server."**
4. Do the few steps it asks of you: create the Discord application, provide the token, invite the bot, run `/setup`.

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
The repository is private, so checks and downloads use **your own** Git access. For in-Discord notifications without the host check, set `UPDATE_REPO` and your own read-only `UPDATE_GITHUB_TOKEN` in `.env`.

**ZIP installs:** get the new ZIP, extract it to a new folder, copy your `.env` into it, run `docker compose down` in the old folder and `docker compose up -d --build` in the new one. Keep the old folder until the new version runs (that's your rollback).

Your configuration and data live in the Docker volume `virtual-room-bot_vrbot-data`, never in the code folder, so updates keep them. Database migrations run automatically and only add.

## Backup
```
docker compose exec bot python -m vrbot.cli backup     # copy of the database in /data/backups
docker run --rm -v virtual-room-bot_vrbot-data:/data -v "$PWD":/out alpine tar czf /out/vrbot-data.tgz -C /data .
```

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

## Notices
See `NOTICE.md` and `docs/THIRD_PARTY.md`.
