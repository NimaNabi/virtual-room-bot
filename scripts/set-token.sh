#!/usr/bin/env bash
# Store the Discord bot token in .env without echoing it, verify it with Discord, then (re)start the bot.
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] || bash scripts/init-env.sh
read -rsp "Paste the Discord bot token (input hidden): " TOKEN; echo
[ -n "$TOKEN" ] || { echo "Empty token, nothing changed."; exit 1; }
TOKEN=$(printf '%s' "$TOKEN" | tr -d '[:space:]"'"'")
H=$(( ${#TOKEN} / 2 )); if [ $(( ${#TOKEN} % 2 )) -eq 0 ] && [ "${TOKEN:0:$H}" = "${TOKEN:$H}" ]; then TOKEN=${TOKEN:0:$H}; echo "(duplicate paste detected and fixed)"; fi
[ "$(printf '%s' "$TOKEN" | awk -F. '{print NF}')" = 3 ] || { echo "That does not look like a bot token (expected three dot-separated parts)."; exit 1; }
code=$(printf 'Authorization: Bot %s' "$TOKEN" | curl -s -o /dev/null -w '%{http_code}' -H @- https://discord.com/api/v10/users/@me)
[ "$code" = 200 ] || { echo "Discord rejected this token (HTTP $code). Nothing changed. Reset the token in the Developer Portal and try again."; exit 1; }
tmp=$(mktemp); grep -v '^DISCORD_TOKEN=' .env > "$tmp" || true
printf 'DISCORD_TOKEN=%s\n' "$TOKEN" >> "$tmp"; mv "$tmp" .env; chmod 600 .env 2>/dev/null || true
unset TOKEN
echo "Token saved to .env. Starting..."
docker compose up -d --build
