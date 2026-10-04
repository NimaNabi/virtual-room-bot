#!/usr/bin/env bash
# Host-side updater. The running bot never replaces its own code; this script does, safely.
#
#   bash scripts/update.sh check              # find the latest release, tell the bot (owner sees it in 🎛️ → Owner → Updates)
#   bash scripts/update.sh apply [vX.Y.Z]     # backup → fetch tag → rebuild → health check → rollback on failure
#   bash scripts/update.sh rollback           # return to the previous known-good version (code + database backup)
#   bash scripts/update.sh watch              # apply update requests made with the Update button (runs until stopped)
#
# Git installations use YOUR Git access to the repository (private repositories need your own credentials).
# ZIP installations can't fetch releases: get the new ZIP and follow "Updating" in README.md.
set -euo pipefail
cd "$(dirname "$0")/.."
DC="docker compose"

say() { printf '%s\n' "$*"; }
die() { say "ERROR: $*" >&2; exit 1; }
is_git() { [ -d .git ] && git rev-parse --git-dir >/dev/null 2>&1; }
current() { tr -d '[:space:]' < VERSION; }
in_bot() { $DC exec -T bot "$@"; }
now() { date +%s; }

write_data() {  # write_data <file> <json>   (inside the persistent volume)
  printf '%s' "$2" | in_bot sh -c "cat > /data/$1"
}
read_data() { in_bot sh -c "cat /data/$1 2>/dev/null" || true; }

latest_tag() {
  git fetch --tags --quiet origin || die "cannot reach the repository (check your Git access)"
  git tag -l 'v[0-9]*' | sort -V | tail -1
}

health_ok() {  # bot healthy + config valid + Control Center registered
  local cid status i
  for i in $(seq 1 36); do
    cid=$($DC ps -q bot)
    status=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$cid" 2>/dev/null || echo none)
    if [ "$status" = "healthy" ]; then
      in_bot python -m vrbot.cli check-config >/dev/null || return 1
      in_bot python -c "import json,sys; m=json.load(open('/data/heartbeat.json')).get('modules',{}); sys.exit(0 if m.get('app') in ('loaded','ok') else 1)" || return 1
      if grep -q '^COMPOSE_PROFILES=.*music' .env 2>/dev/null; then
        lstatus=$(docker inspect --format '{{.State.Health.Status}}' "$($DC ps -q lavalink)" 2>/dev/null || echo none)
        [ "$lstatus" = "healthy" ] || { sleep 5; continue; }
      fi
      return 0
    fi
    sleep 5
  done
  return 1
}

state_json() {  # state_json <current> <previous> <result> <success_ts>
  printf '{"install":"git","current":"%s","previous":"%s","last_result":"%s","last_success":%s,"last_attempt":%s}' \
    "$1" "$2" "$3" "${4:-null}" "$(now)"
}

cmd_check() {
  if ! is_git; then
    say "This is a ZIP installation: releases can't be fetched automatically."
    say "Ask for / download the newest package, then follow 'Updating' in README.md."
    write_data update-state.json "{\"install\":\"zip\",\"current\":\"$(current)\"}" || true
    return 0
  fi
  local tag notes
  tag=$(latest_tag)
  [ -n "$tag" ] || die "no release tags found"
  notes=$(git show "$tag:release.json" 2>/dev/null || echo '{}')
  write_data update-check.json "$(python3 -c "import json,sys,time;m=json.loads(sys.argv[2] or '{}');print(json.dumps({'version':sys.argv[1].lstrip('v'),'notes':m.get('notes',[]),'breaking':m.get('breaking',False),'min_config_schema':m.get('min_config_schema'),'checked_at':time.time(),'source':'host'}))" "$tag" "$notes")"
  say "Installed: $(current) · Latest: ${tag#v}"
}

cmd_apply() {
  is_git || die "ZIP installation: download the new package and follow 'Updating' in README.md."
  [ -z "$(git status --porcelain --untracked-files=no)" ] || die "local code changes found — commit or discard them first"
  local target prev backup
  target=${1:-$(latest_tag)}
  [ -n "$target" ] || die "no release tags found"
  git rev-parse -q --verify "refs/tags/$target" >/dev/null || git fetch --tags --quiet origin
  git rev-parse -q --verify "refs/tags/$target" >/dev/null || die "unknown release $target"
  prev=$(git describe --tags --exact-match 2>/dev/null || git rev-parse --short HEAD)
  if [ "v$(current)" = "$target" ] || [ "$(current)" = "$target" ]; then say "Already on $target."; return 0; fi
  if git show "$target:release.json" 2>/dev/null | grep -q '"breaking": *true'; then
    say "⚠️  $target is marked BREAKING — read its release notes (CHANGELOG.md) before continuing."
    [ "${FORCE:-}" = 1 ] || die "re-run with FORCE=1 to apply a breaking release"
  fi
  say "1/5 backup"
  backup=$(in_bot python -m vrbot.cli backup | sed -n 's/^backup written: //p')
  in_bot sh -c "cp /data/server.yaml /data/backups/server-before-$target.yaml 2>/dev/null; echo $(current) > /data/backups/version-before-$target.txt"
  write_data update-rollback.json "{\"previous\":\"$prev\",\"db_backup\":\"$backup\"}"
  say "2/5 switch code to $target (from $prev)"
  git -c advice.detachedHead=false checkout --quiet "$target"
  say "3/5 rebuild + restart (build log: /tmp/vrbot-update.log)"
  $DC up -d --build >/tmp/vrbot-update.log 2>&1 || say "build/start reported an error (see /tmp/vrbot-update.log)"
  say "4/5 health check (bot, Discord, database, config, Control Center, Lavalink)"
  if health_ok; then
    write_data update-state.json "$(state_json "$(current)" "$prev" ok "$(now)")"
    in_bot sh -c "rm -f /data/update-request.json"
    say "5/5 ✅ updated to $target (previous: $prev)"
  else
    say "5/5 ❌ health check failed — rolling back to $prev"
    do_rollback "$prev" "$backup" "failed health check on $target"
    die "update to $target failed and was rolled back; backup kept at $backup"
  fi
}

do_rollback() {  # do_rollback <ref> <db_backup> <reason>
  git -c advice.detachedHead=false checkout --quiet "$1"
  $DC stop bot >/dev/null
  if [ -n "${2:-}" ]; then $DC run --rm --no-deps bot sh -c "cp '$2' /data/vrbot.db" >/dev/null; fi
  $DC up -d --build >>/tmp/vrbot-update.log 2>&1 || true
  if health_ok; then
    write_data update-state.json "$(state_json "$(current)" "" "rolled back: $3" "")" || true
    say "rolled back to $1"
  else
    write_data update-state.json "$(state_json "$(current)" "" "rolled back (not healthy yet — check token/logs): $3" "")" || true
    say "Rolled back to $1, but the bot is not healthy yet (check 'docker compose logs bot'). Backups: /data/backups."
  fi
}

cmd_rollback() {
  is_git || die "ZIP installation: keep the previous folder and run 'docker compose up -d --build' in it."
  local info prev db
  info=$(read_data update-rollback.json)
  prev=$(printf '%s' "$info" | python3 -c "import json,sys;print(json.load(sys.stdin).get('previous',''))" 2>/dev/null || true)
  db=$(printf '%s' "$info" | python3 -c "import json,sys;print(json.load(sys.stdin).get('db_backup',''))" 2>/dev/null || true)
  [ -n "$prev" ] || die "no previous version recorded"
  do_rollback "$prev" "$db" "manual rollback"
}

cmd_watch() {
  say "Watching for update requests from the 🎛️ Owner → Updates button (Ctrl+C to stop)…"
  while true; do
    req=$(read_data update-request.json)
    if [ -n "$req" ]; then
      ver=$(printf '%s' "$req" | python3 -c "import json,sys;print(json.load(sys.stdin).get('version',''))")
      if [ -n "$ver" ]; then ( cmd_apply "v${ver#v}" ) || say "update to $ver did not complete (see messages above)"; fi
      in_bot sh -c "rm -f /data/update-request.json" || true
    fi
    sleep 60
  done
}

case "${1:-}" in
  check) cmd_check ;;
  apply) shift; cmd_apply "${1:-}" ;;
  rollback) cmd_rollback ;;
  watch) cmd_watch ;;
  *) sed -n '2,10p' "$0"; exit 2 ;;
esac
