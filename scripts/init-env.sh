#!/usr/bin/env bash
# Create .env from .env.example with a random Lavalink password (does nothing if .env exists).
set -euo pipefail
cd "$(dirname "$0")/.."
if [ -f .env ]; then echo ".env already exists — not touching it."; exit 0; fi
cp .env.example .env
pw=$(python3 -c "import secrets;print(secrets.token_urlsafe(32))" 2>/dev/null || openssl rand -hex 24)
sed -i.bak "s/^LAVALINK_PASSWORD=.*/LAVALINK_PASSWORD=$pw/" .env && rm -f .env.bak
chmod 600 .env 2>/dev/null || true
echo "Created .env (random Lavalink password set). Now add DISCORD_TOKEN (or run scripts/set-token.sh)."
