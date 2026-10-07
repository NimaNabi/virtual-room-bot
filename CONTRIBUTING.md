# Contributing

Thanks for helping! Small, focused pull requests are easiest to review.

## Local development
```
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest -q                               # the whole suite runs offline, no token needed
```
To run the bot itself, use a **test** Discord application and a throwaway server (never your real token in tests):
`docker compose up -d --build` with your test token in `.env`.

## Where things live
See [ARCHITECTURE.md](ARCHITECTURE.md). In short: one cog per feature in `vrbot/cogs/`, shared logic in `vrbot/`
(permissions in `vrbot/perms/`, identity and rendering in `identity.py` / `render.py`, storage in `db.py` with
numbered SQL migrations in `vrbot/migrations/`).

## Pull request expectations
- Tests for new behaviour (`tests/`), and the full suite passing.
- Database changes as a **new** numbered migration that only adds; never edit an existing migration.
- Every privileged action is authorized on the server side, not just hidden in the UI.
- Log people through the identity helpers (`vrbot/identity.py`), never by hand-formatting names.
- No secrets, personal IDs or server-specific names in code, tests or examples.
- New user-facing features are optional and configurable; existing behaviour stays unchanged unless discussed.
- Keep the button-first Control Center consistent: Back/Home on every screen, loading and error feedback.

## Reporting bugs
Open an issue with the version (`VERSION`), what you did, what you expected and the relevant
`docker compose logs bot` lines (remove tokens and IDs first). Security problems: see [SECURITY.md](SECURITY.md).
