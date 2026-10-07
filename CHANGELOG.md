# Changelog

## 1.0.6 — 2026-10-07
- Public release: MIT license, SECURITY.md, CONTRIBUTING.md, ARCHITECTURE.md
- README rewritten for first-time visitors (where the bot runs, quick start)
- update checks work without a token against the public repository (set UPDATE_REPO)

## 1.0.5 — 2026-10-04
- Every person in every log is a native Discord mention: one clickable @Name that opens the profile, with no link, preview card or raw ID, and nobody is ever pinged by a log entry
- the user ID stays stored and searchable in Owner → Logs → Find person

## 1.0.4 — 2026-10-04
- Clickable identities in logs never expand into profile cards or link previews: each person stays one compact @Name that opens their Discord profile

## 1.0.3 — 2026-10-04
- Logs show each person as one clean clickable @Name (actor and target) that opens their Discord profile via the immutable user ID, instead of name + @username + raw ID
- raw IDs stay stored and are shown on demand in Owner → Logs → Find person
- the inviter in join/leave lines is clickable too
- link previews are suppressed in the owner log channels

## 1.0.2 — 2026-10-04
- Global identity standard: every person in every log (target and actor) is stored with a snapshot (user ID, @username, names) and every owner log line shows the immutable ID
- Owner → Logs → Find person finds anyone by user ID or any past name or username (also after renames or leaving)
- temporary-room actions (trust, untrust, block, unblock, invite, disconnect, settings), configuration reloads, update requests and setup runs are now logged with who did it
- trust-level changes show old → new level, temporary access shows kind, duration and expiry
- fix: /logs no longer fails on guest/invite events
- the room panel shows Invite a friend only when the room owner may invite guests

## 1.0.1 — 2026-10-04
- Member join/leave/kick/ban logs now record a permanent identity: user ID, @username, global name, account creation date, join date, last trust level, roles and inviter
- the removal cause is only stated when the audit log proves it
- Owner → Logs → Find person searches by name, @username or user ID (also after someone left)
- Owner → Updates shows installed/latest version and release notes
- host-side scripts/update.sh (check / apply with backup, health check and automatic rollback / rollback / watch)

## 1.0.0 — 2026-10-03
- First release.
