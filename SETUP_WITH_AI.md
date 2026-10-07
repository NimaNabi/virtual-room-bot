# Setup instructions for an AI coding agent

You are configuring a fresh installation of this Discord bot for the current user.

## Getting the code
If this folder is not yet a checkout, run `git clone https://github.com/NimaNabi/virtual-room-bot.git` (or use the
release ZIP). Prefer a Git checkout: it makes `scripts/update.sh` updates and rollbacks possible.

## Rules
- Inspect the local machine and this project before changing anything.
- Set up every non-secret dependency you can safely configure yourself.
- Do not assume any pre-existing Discord server, user, role or channel IDs. Do not reuse example IDs from the docs.
- Ask the human only when a credential, a choice, or an owner-only Discord action is required.
- Never print, log, commit or upload secrets: the bot token, `LAVALINK_PASSWORD` and AI keys. Write them only to `.env`.
- Configure this installation for the user's own Discord server.
- Run the tests and verify the deployment before declaring completion.

## Steps
1. **Inspect the system.** Check the OS and shell, `docker version` and `docker compose version`. If Docker is missing, explain how to install Docker Desktop (Windows/macOS) or Docker Engine (Linux) and wait. Never install system software without consent.
2. **Create `.env`.** Run `bash scripts/init-env.sh`. Without bash, copy `.env.example` to `.env` and set `LAVALINK_PASSWORD` to a random string of 32+ characters you generate.
3. **Discord application.** The human does this; guide them step by step.
   1. Open https://discord.com/developers/applications → **New Application**. The name they choose is the bot's name.
   2. **Bot** page: turn on **Server Members Intent**. Leave Message Content and Presence off unless they want those features.
   3. **Bot** page: click **Reset Token** and copy the token.
   4. **General Information** page: copy the **Application ID** (not secret).
4. **Token.** Ask the human to run `bash scripts/set-token.sh` (hidden input, verified with Discord), or to put the token into `.env` as `DISCORD_TOKEN=`. Avoid having the token pasted into the chat.
5. **Options.** Ask whether they want the AI assistant.
   - If not, leave `AI_ENABLED=false`.
   - If yes, they supply an OpenAI-compatible `AI_BASE_URL`, `AI_API_KEY` and `AI_MODEL`.
   - Set `GUILD_ID` only if the bot will be in more than one server.
   - Ask whether they want daily update notifications; if yes set `UPDATE_REPO=NimaNabi/virtual-room-bot` (no token).
   - Remind them: the bot is online only while this computer is on and connected.
6. **Start.** Run `docker compose up -d --build`. Wait until `docker compose ps` shows the bot (and lavalink, if music is on) running or healthy. Read `docker compose logs bot` and fix configuration errors. Migrations run automatically.
7. **Invite.** Run `docker compose exec bot python -m vrbot.cli invite` and give the human the printed link. They pick their server, then drag the bot's role above the roles it should manage (Server Settings → Roles).
8. **Configure the server.** The human runs **/setup** in Discord (server owner only).
   - Before they start, ask them for: the menu name; the three trust level names (or "keep defaults"); optional colours; whether to reuse existing roles; the layout; and which modules to enable.
   - Layout guidance: **Minimal** for a busy existing server, **Recommended** for a new or empty server, **Existing server** to reuse their roles.
   - They review the preview, press **Apply**, then **Give default level…**.
9. **Check the menu.** Setup publishes the 🎛️ menu in the menu channel. Confirm the human sees it.
10. **Test music.** The human joins a voice channel, taps **🎵 Music → ▶️ Quick Play → 😌 Chill**, hears audio, then taps Pause and Resume.
11. **Test a temporary room.** The human joins **➕ Create Room**, gets moved into their own room with a control panel, taps **Lock** then **Unlock**, and leaves. The room should disappear.
12. **Test permissions and run the suite.** The human runs `/server doctor` and `/permissions privacy`; report any CRITICAL findings. Run `docker compose exec bot python -m pytest -q` and confirm all tests pass.
13. **Report.** Tell the human:
    - what was installed;
    - where data lives (the Docker volume `virtual-room-bot_vrbot-data`);
    - which modules are on;
    - how to update and back up (see README);
    - anything that still needs their action.
