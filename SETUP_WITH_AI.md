# Setup instructions for an AI coding agent

You are configuring a fresh installation of this Discord bot for the current user. Follow this file from top to
bottom; it is complete — no knowledge beyond this repository is needed.

## Getting the code
If this folder is not yet a checkout, run `git clone https://github.com/NimaNabi/virtual-room-bot.git` (or use the
release ZIP from the Releases page).

## Rules
- Inspect the local machine and this project before changing anything.
- Set up every non-secret dependency you can safely configure yourself. Ask before downloading or installing software.
- Do not assume any pre-existing Discord server, user, role or channel IDs. Do not reuse example IDs from the docs.
- Ask the human only when a credential, a choice, or an owner-only Discord action is required.
- Never print, log, commit or upload secrets: the bot token, `LAVALINK_PASSWORD` and AI keys. Write them only to `.env`.
  **Never ask the human to paste the token into the chat**; they type it into a hidden prompt themselves.
- Run the tests and verify the installation before declaring completion.
- Tell the human early: the bot is online only while the computer it runs on is on and connected.

## 1. Choose the install path
Check the OS (and on Windows whether Docker is installed: `docker version`).

| Situation | Path |
|---|---|
| Windows 10/11, Docker **not** installed (most people) | **A. Windows (native)** |
| macOS, Linux, a server/VPS, or Docker already installed | **B. Docker** |

If both are possible, recommend A on a personal Windows PC and B on servers.

## 2. Discord application (human, both paths)
Guide the human step by step:
1. Open https://discord.com/developers/applications → **New Application**. The name they choose is the bot's name.
2. **Bot** page: turn on **Server Members Intent** and click **Save Changes**. Leave Message Content and Presence off
   unless they want AutoMod (needs Message Content) or presence logs.
3. **Bot** page: click **Reset Token** and copy the token. They will paste it into a hidden prompt in a moment.
(The Application ID is not needed: the tools derive it from the token.)

## 3. Options (ask, both paths)
Ask, then edit `.env` (create it first: path A creates it in step A1, path B in step B2):
- **AI assistant?** If not, leave `AI_ENABLED=false`. If yes, they supply an OpenAI-compatible `AI_BASE_URL`,
  `AI_API_KEY` and `AI_MODEL` (they may type the key into `.env` themselves).
- **Music?** On by default (`COMPOSE_PROFILES=music`). To turn it off, set `COMPOSE_PROFILES=` (empty).
- **Update notifications in Discord?** If yes, `UPDATE_REPO=NimaNabi/virtual-room-bot` (no token needed).
- **Timezone** for the nightly backup (Docker only; the Windows path uses the PC's own clock): `TZ=` e.g. `Europe/Berlin`.
- `GUILD_ID` only if the bot will be in more than one server.

## A. Windows (native, no Docker)
Everything goes into this folder; nothing is installed system-wide and no administrator rights are needed.
- **A1. Runtimes.** Ask first ("about 160 MB from nuget.org, github.com and pypi.org, checked against fixed
  checksums"), then run:
  `powershell -NoProfile -ExecutionPolicy Bypass -File installer\setup.ps1 -RuntimeOnly`
  Every line must start with `[OK]`. If a download is blocked (antivirus, proxy), report the `[X]` line and stop.
  Then create `.env` if it doesn't exist: `copy .env.example .env` (the manager generates `LAVALINK_PASSWORD` itself).
  Apply the options from step 3.
- **A2. Token, start, invite — the human runs this** (it has a hidden token prompt; you cannot type it for them):
  double-click `Setup.cmd`, or in a terminal: `runtime\python\python.exe installer\vrb.py configure`.
  It checks the token with Discord, waits until Server Members Intent is on, starts the bot and music service, opens
  the invite link, and offers "start with Windows" and a desktop shortcut.
- **A3. Check:** `runtime\python\python.exe installer\vrb.py status` must show `Bot: running`.
  Invite link again: `runtime\python\python.exe installer\vrb.py invite`. Logs: `... installer\vrb.py logs 60`.
- **A4. Tests:** `runtime\python\python.exe -m pip install -r requirements-dev.txt` then
  `runtime\python\python.exe -m pytest -q` — all must pass.
- Data lives in the `data\` folder (database, `server.yaml`, `backups\`); logs in `logs\`. Continue with step 4.

## B. Docker
- **B1. Docker.** `docker version` and `docker compose version`. If missing, explain how to install Docker Desktop
  (Windows/macOS) or Docker Engine (Linux) and wait. Never install system software without consent.
- **B2. `.env`.** Run `bash scripts/init-env.sh` (on Windows, Git for Windows provides `bash`). Without bash: copy
  `.env.example` to `.env` and set `LAVALINK_PASSWORD` to a random string of 32+ characters you generate.
  Apply the options from step 3.
- **B3. Token.** Ask the human to run `bash scripts/set-token.sh` (hidden input, verified with Discord), or to type the
  token into `.env` as `DISCORD_TOKEN=` themselves.
- **B4. Start.** `docker compose up -d --build`. Wait until `docker compose ps` shows the bot (and lavalink, if music
  is on) healthy. If not: `docker compose logs bot` and `docker compose exec bot python -m vrbot.cli check-config`.
- **B5. Invite.** `docker compose exec bot python -m vrbot.cli invite` and give the human the printed link.
- **B6. Tests.** `docker compose exec bot python -m pytest -q` — all must pass.
- Data lives in the Docker volume `virtual-room-bot_vrbot-data`. Continue with step 4.

## 4. Add the bot to the server (human)
They open the invite link, pick their server and click Authorize, then in **Server Settings → Roles** drag the bot's
role above the roles it should manage.

## 5. Configure the server (human, in Discord)
The server owner types **/setup**. Five short pages: identity (menu name, nickname), trust levels (keep the defaults,
rename/recolour, or reuse existing roles), layout, modules, preview → **Apply**, then **Give default level…**.
Layout guidance to offer: **Minimal** for a busy existing server, **Recommended** for a new or empty server,
**Existing server** to reuse their roles. (They type the choices in Discord; you only advise.)

## 6. Verify (human, in Discord; you collect the results)
1. The 🎛️ menu appears in the menu channel.
2. Music: join a voice channel → **🎵 Music → ▶️ Quick Play → 😌 Chill**, audio plays; Pause and Resume work.
3. Temporary room: join **➕ Create Room** → moved into an own room with a control panel; Lock, then Unlock; leave →
   the room disappears.
4. `/server doctor` and `/permissions privacy`: report any CRITICAL findings.
5. Owner → **System** shows the bot online and a backup schedule.

## 7. Report
Tell the human: the install path used; where data lives; which modules are on; how to start/stop/update/back up
(path A: the desktop shortcut menu; path B: README); that the bot is online only while this computer is on; and
anything that still needs their action.
