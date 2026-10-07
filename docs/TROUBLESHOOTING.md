# Troubleshooting

**Never post your bot token, `LAVALINK_PASSWORD` or API keys anywhere** (issues, Discord, screenshots). If a token
was exposed, reset it in the Developer Portal right away.

## Where to look first
| | Windows (Setup.cmd install) | Docker |
|---|---|---|
| Status | desktop menu → **Status** | `docker compose ps` |
| Logs | menu → **View logs** (files in `logs\`) | `docker compose logs --tail 100 bot` |
| Diagnostics for an issue | menu → **Diagnostics** (saved to `data\diagnostics.txt`) | `docker compose logs --tail 300 bot \| docker compose exec -T bot python -m vrbot.cli diagnostics --log` |

The diagnostics report contains versions, platform, bot state, migrations, module status and recent warnings/errors
with IDs, e-mails and anything token-like masked. It never contains the token, keys, message text or member data.
Read it before posting anyway.

## Common problems
**Bot offline**
- The bot runs only while its computer is on and online.
- Windows: Status shows *Recovery: gave up* → the bot crashed repeatedly; **View logs** shows why. Fix the cause,
  then **Start**. Docker: `docker compose logs bot`.

**"DISCORD_TOKEN is not set" / "Invalid token"**
Reset the token (Developer Portal → your application → Bot → Reset Token). Windows: menu → **Change bot token**,
then **Restart**. Docker: `bash scripts/set-token.sh` then `docker compose up -d`.

**"Privileged intents" error / no member events**
Developer Portal → Bot → turn on **Server Members Intent** → Save. Turn on **Message Content Intent** only if you
set `MESSAGE_CONTENT_INTENT=true` (needed for AutoMod); **Presence Intent** only with `PRESENCE_INTENT=true`.

**Music unavailable**
- Windows: Status must show *Music: running*. If not, **View logs** and open `logs\lavalink.log`. The music service
  needs `runtime\java` and `runtime\lavalink` — run `Setup.cmd` again to repair them (finished steps are skipped).
- Docker: `docker compose ps` must show `lavalink` healthy; check `COMPOSE_PROFILES=music` and `LAVALINK_PASSWORD`
  in `.env`; `docker compose logs lavalink`.
- Some tracks or sources can be unavailable from time to time; try radio or another search.

**`/setup` reports missing permissions**
Re-invite with the invite link (Windows menu → **Invite link**; Docker `python -m vrbot.cli invite`), or give the
bot's role those permissions, and drag it above the roles it should manage.

**Windows SmartScreen / antivirus warning**
`Setup.cmd` is a plain script that downloads Python (nuget.org), Java (Adoptium on github.com), Lavalink (github.com)
and Python packages (pypi.org), each checked against a fixed checksum. Only run it from a ZIP you downloaded from
this repository's Releases page. If an antivirus blocks a download, Setup stops with an `[X]` line and changes nothing
outside the folder.

**Update failed**
Updates roll back automatically when the new version doesn't start healthy; your database backup is kept in
`backups`. Windows: menu → Status/View logs, then try again later or report the issue with Diagnostics. Docker:
`bash scripts/update.sh rollback` returns to the previous version manually.

**Configuration errors**
Docker: `docker compose exec bot python -m vrbot.cli check-config`. Windows: View logs.

## Re-run setup safely
- Windows: running `Setup.cmd` again is safe — runtimes and packages are reused, your `.env` and `data\` stay.
- Discord side: `/setup` can be run again; it shows a preview before changing anything.
- Start completely fresh (deletes all bot data!): stop the bot, back up, then delete `data\` (Windows) or run
  `docker compose down -v` (Docker).

## Restore a backup
Backups are in `data\backups\` (Windows) or `/data/backups` (Docker): `auto-<time>.db` nightly, `manual-<time>.db`
from **Backup now** or before updates.
- Windows: menu → **Stop**, then in the bot folder: `runtime\python\python.exe -m vrbot.cli restore <file>`
  (run it with `DATA_DIR=data`, e.g. in PowerShell: `$env:DATA_DIR='data'; runtime\python\python.exe -m vrbot.cli restore auto-20261007-040000.db`), then **Start**.
- Docker: `docker compose stop bot`, `docker compose run --rm bot python -m vrbot.cli restore <file>`, `docker compose up -d bot`.

Restore checks the backup's integrity, asks you to type RESTORE, and keeps the current database as `pre-restore-…`.

## Still stuck?
[Open an issue](https://github.com/NimaNabi/virtual-room-bot/issues/new/choose) with the diagnostics report.
