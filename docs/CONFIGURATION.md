# Configuration

## Where things live
| What | Where | Changed by updates? |
|---|---|---|
| Application code | this folder | yes |
| Secrets and switches | `.env` in this folder | no (keep it) |
| Server configuration | `/data/server.yaml` in the `virtual-room-bot_vrbot-data` volume | no |
| Database, snapshots, backups | `/data` in the same volume | no (migrations only add) |

Edit the runtime configuration:
```
docker compose cp bot:/data/server.yaml ./server.yaml
# edit server.yaml
docker compose cp ./server.yaml bot:/data/server.yaml
```
Then run `/baseline reload` in Discord, or `docker compose restart bot`.

## Schema overview (`server.yaml`)
| Section | Purpose |
|---|---|
| `identity` | Menu name, subtitle, the bot's nickname in your server |
| `trust` | Level names, colours, extra permissions per level, which levels may create guest invites |
| `layout.names` | Names `/setup` uses for channels and categories it creates |
| `onboarding.rules` | The rules post |
| `baseline` | Level role IDs (filled in by `/setup`) and the permission baseline the auditor checks |
| `privacy` | Owner, owner-equivalents, area of each channel/category (filled in by `/setup`), social-privacy checks |
| `guardian`, `autoheal`, `voice_rooms`, `music`, `summaries`, `retention`, `security`, `ai` | Module settings |

## Trust levels
| Key (security identity) | Default name | Sees |
|---|---|---|
| `trust_level_1` (highest normal trust) | Trusted | Level 1 + 2 + 3 areas and public |
| `trust_level_2` | Standard | Level 2 + 3 areas and public |
| `trust_level_3` (default) | Member | Level 3 area and public |
| owner | — | everything, including the owner area and logs |

- Names and colours are display only. Renaming a level never changes what it is allowed to do.
- Each member holds exactly one level role.
- Change a member's level with `/tier manage` or **🎛️ → Owner → pick a member**.
- `trust.permissions` adds Discord permissions to a level on top of the generic preset. Higher levels automatically include lower ones.
- Dangerous permissions (Administrator, Manage *, kick, ban, timeout, move, mute, deafen, …) are always refused for levels. Use **temporary access** (`/access`), which always expires.

## Owners
- **Owner:** the server owner, unless `OWNER_ID` is set in `.env`.
- **Owner-equivalents:** accounts listed in `OWNER_EQUIVALENT_IDS` (for example your own second account) get full owner visibility.
- **Other admins:** any other account with Administrator is reported as a privacy risk.

## Areas
`privacy.areas` maps channel or category IDs to an area: `owner_area`, `trust_level_1`, `trust_level_2`, `trust_level_3` or `public`. `/setup` fills it in for everything it creates. Add channels you make yourself, then check with `/permissions privacy`.

## Optional features
- **AI assistant:** set `AI_ENABLED=true`, `AI_BASE_URL`, `AI_API_KEY` and `AI_MODEL` in `.env`.
- **Auto-heal:** set `autoheal.enabled: true` after `/setup`. It reverts unambiguous permission drift from the recorded baseline and alerts you about the rest.
- **Social privacy checks:** with `privacy.social_privacy: true`, level roles that reveal a ranking (names, hoisting, colours) are reported.
- **Presence log:** turn on Presence Intent in the Developer Portal, then set `PRESENCE_INTENT=true`.
- **Music off:** remove `music` from `COMPOSE_PROFILES` and set `music.enabled: false`.
