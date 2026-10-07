# Architecture

A high-level map for developers. Python 3.12, discord.py 2.7, SQLite (aiosqlite), Lavalink 4 via wavelink.

## Runtime
```
docker compose
 ├─ bot        python -m vrbot   (all features, one process)
 └─ lavalink   audio node        (only with the "music" profile)
volume vrbot-data → /data        server.yaml, database, backups, update state
```
Nothing about your server is hard-coded: `config/default.yaml` is copied to `/data/server.yaml` on first start and
`/setup` fills it in (role, channel and category IDs are discovered or created by the wizard).

## Layers
| Layer | Files | Role |
|---|---|---|
| Entry / wiring | `bot.py`, `__main__.py`, `cli.py` | loads config, database, cogs; CLI for invite link, backup, config check |
| Configuration | `config.py`, `config/default.yaml` | validated server config (pydantic) + `.env` switches |
| Storage | `db.py`, `migrations/*.sql`, `musicdb.py` | numbered additive migrations, key/value store, structured events |
| Identity & rendering | `identity.py`, `render.py`, `events.py` | every logged person is stored by immutable user ID with a snapshot; logs are a view of stored events |
| Authorization | `authz.py`, `trust.py`, `perms/` | owner/trust/elevation checks; permission model, privacy audit, snapshots |
| Feature registry | `features.py` | one list of modules (audience, on/off, setup needs) → Help, Owner → System |
| Runtime health | `uptime.py` | downtime classification, backup schedule and SQLite-safe snapshots (pure, tested) |
| UI | `ui.py`, `cogs/app.py` | the 🎛️ Control Center: persistent buttons (`DynamicItem`), navigation stack, Back/Home |
| Features | `cogs/*.py` | one cog per feature (below) |

## Feature cogs
| Area | Cogs |
|---|---|
| Control Center & help | `app`, `help`, `onboarding`, `welcome` |
| Music | `music` (+ `musicdb`) |
| Temporary rooms | `voicerooms` |
| Trust, guests, temporary access | `tier`, `guests`, `invites`, `access`, `zero` |
| Moderation | `moderation` |
| Logging | `eventlog` (Discord events → stored events), `ownerlogs` (owner-only log channels), `logs` (search) |
| Security | `guardian`, `permissions`, `server`, `baseline` (+ `guardian_rules`, `repair`, `autoheal_policy`) |
| Social | `fun`, `stats`, `presence` |
| Moderation add-ons | `automod` (optional word/invite filter) |
| Owner / ops | `system` (downtime reports, nightly DB backups, server added/removed), `backup` (structure snapshots), `updates`, `setup` |
| AI (optional) | `ai` (+ `ai/`) |

## Key flows
- **Event logging:** a Discord event → `eventlog` → `db.add_event()` attaches actor/target identity snapshots →
  hooks render it for the owner log channels; `/logs` and *Find person* search the stored rows.
- **Buttons:** custom IDs encode the action (`va:<action>:<arg>`), so buttons keep working after restarts; every
  handler re-checks permissions before acting.
- **Updates:** the bot only *notifies*. `scripts/update.sh` (host side) does backup → checkout of a release tag →
  rebuild → health check → rollback on failure.

## Tests
`tests/` runs fully offline with fakes for Discord objects: `python -m pytest -q`.
