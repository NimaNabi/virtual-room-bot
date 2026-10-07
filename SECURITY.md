# Security policy

## Reporting a vulnerability
Please report security problems **privately** through GitHub: open the repository's **Security** tab and choose
**Report a vulnerability** (private vulnerability reporting). Do not open a public issue for an unfixed vulnerability.

Include what you found, how to reproduce it, and which version (`VERSION` file) you tested. You will get an answer as
soon as possible; fixes ship as a normal release with a note in `CHANGELOG.md`.

## Supported versions
Only the latest release receives fixes. Update with `scripts/update.sh` (see README).

## Security model
The source code is public, so security never depends on secrecy of the implementation. It relies on:
- **Discord authorization.** Every button and command re-checks the clicking member on the server side; hiding a
  button is a convenience, never the protection.
- **Owner checks.** Owner tools require the server owner (or IDs in `OWNER_IDS`); temporary authority always expires.
- **Secret isolation.** The bot token, `LAVALINK_PASSWORD` and AI keys live only in `.env` (never committed, never
  logged). The repository contains no credentials of any kind.
- **Least privilege.** The invite link requests only the permissions the enabled features need; `/server doctor`
  reports anything broader.
- **No self-modification.** The running bot never downloads or executes code. Updates are applied by the host
  updater after the owner approves them, with a backup, health check and automatic rollback.

## What stays local
The database, logs and backups stay on your machine. The bot contacts only Discord, the music/radio sources you play,
GitHub's public API if you enable update checks (`UPDATE_REPO`), and an AI API if you configure one. No telemetry.
