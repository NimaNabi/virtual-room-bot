"""Readable permissions, strict audit attribution, downtime, scheduled backups, AutoMod, feature registry."""
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from vrbot import features, uptime as U
from vrbot.cogs.automod import blocked_word, invite_codes, normalize, parse_words
from vrbot.config import ServerConfig
from vrbot.db import Database
from vrbot.events import VoiceAttributor, category_of
from vrbot.perms.flags import discord_name, overwrite_states, readable_changes
from vrbot.render import event_line, owner_log_line

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------- readable permission changes
def test_official_permission_names():
    assert discord_name("moderate_members") == "Timeout Members"
    assert discord_name("manage_guild") == "Manage Server"
    assert discord_name("manage_roles") == "Manage Roles" and discord_name("manage_roles", True) == "Manage Permissions"
    assert discord_name("send_messages_in_threads") == "Send Messages in Threads"


def test_overwrite_transitions():
    ch = {"allow": {"added": ["moderate_members"], "removed": ["connect"]},
          "deny": {"added": ["manage_channels", "connect"], "removed": ["move_members"]}}
    states = {p: (b, a) for p, b, a in overwrite_states(ch)}
    assert states == {"moderate_members": ("default", "allowed"), "connect": ("allowed", "denied"),
                      "manage_channels": ("default", "denied"), "move_members": ("denied", "default")}
    lines = readable_changes(ch)
    assert "✅ Timeout Members — now allowed" in lines
    assert "❌ Manage Channels — now denied" in lines
    assert "❌ Connect — now denied (was allowed)" in lines
    assert "⬜ Move Members — reset to default (was denied)" in lines
    assert not any("moderate_members" in line for line in lines)   # never internal names


def test_role_permission_lines_and_render():
    lines = readable_changes({"permissions": {"added": ["moderate_members"], "removed": ["kick_members"]}})
    assert lines == ["✅ Timeout Members — now allowed", "❌ Kick Members — no longer allowed"]
    e = {"ts": NOW.isoformat(), "type": "overwrite_update", "category": "structure", "channel_name": "general",
         "actor_id": 5, "actor_name": "Mod", "actor_confidence": "confirmed",
         "details": json.dumps({"overwrite_for": "@everyone",
                                "changes": {"allow": {"added": [], "removed": []},
                                            "deny": {"added": ["send_messages"], "removed": []}}})}
    line = owner_log_line(e)
    assert "❌ Send Messages — now denied" in line and "for **@everyone**" in line and "<@5>" in line
    assert "deny:" not in event_line(e) and "Send Messages — now denied" in event_line(e)


def test_many_changes_are_capped():
    lines = readable_changes({"permissions": {"added": ["kick_members", "ban_members", "administrator", "manage_channels",
                                                        "manage_guild", "add_reactions", "view_audit_log", "stream",
                                                        "connect", "speak"], "removed": []}})
    assert len(lines) == 9 and lines[-1] == "… and 2 more"


# ---------------------------------------------------------------- attribution: never guess
def _entry(i, action, ex, count, ch=None, target=None, age=0):
    return {"id": i, "action": action, "count": count, "channel_id": ch, "target_id": target,
            "executor_id": ex, "executor_name": f"m{ex}", "created_at": NOW - timedelta(seconds=age)}


def test_grouped_moves_credit_by_count_increase():
    va = VoiceAttributor()
    va.prime([{"id": 1, "count": 3}])                       # history: never credited
    va.observe([_entry(1, "member_move", 7, 3, ch=50)], NOW)
    assert va.match("member_move", 50, NOW)[2] == "unknown"
    va.observe([_entry(1, "member_move", 7, 5, ch=50)], NOW)  # same entry bumped twice -> two credits
    assert va.match("member_move", 50, NOW)[:2] == (7, "m7")
    assert va.match("member_move", 50, NOW)[0] == 7
    assert va.match("member_move", 50, NOW)[2] == "unknown"   # credits used up


def test_simultaneous_moderators_are_ambiguous_not_guessed():
    va = VoiceAttributor()
    va.observe([_entry(1, "member_disconnect", 7, 1), _entry(2, "member_disconnect", 8, 1)], NOW)
    ex, name, conf = va.match("member_disconnect", None, NOW)
    assert (ex, name, conf) == (None, None, "ambiguous")
    assert len(va.credits) == 2                               # nothing consumed


def test_message_delete_matches_author_and_channel():
    va = VoiceAttributor()
    va.observe([_entry(9, "message_delete", 7, 1, ch=60, target=111)], NOW)
    assert va.match("message_delete", 61, NOW, target_id=111)[2] == "unknown"   # other channel
    assert va.match("message_delete", 60, NOW, target_id=222)[2] == "unknown"   # other author
    assert va.match("message_delete", 60, NOW, target_id=111)[:2] == (7, "m7")


def test_stale_new_entry_is_not_credited():
    va = VoiceAttributor()
    va.observe([_entry(3, "member_move", 7, 1, ch=50, age=300)], NOW)
    assert va.match("member_move", 50, NOW)[2] == "unknown"


def test_ambiguous_renders_unknown_actor():
    e = {"ts": NOW.isoformat(), "type": "voice_disconnect", "category": "voice", "target_id": 3, "target_name": "B",
         "actor_confidence": "ambiguous", "details": "{}"}
    assert "actor unknown (several moderators" in owner_log_line(e)


# ---------------------------------------------------------------- downtime
def test_downtime_classification():
    started = 1000.0
    assert U.classify(None, None, started, 5000) is None                       # first start ever
    assert U.classify(4900, None, started, 5000) is None                       # short gap: quick restart
    assert U.classify(1500, None, started, 5000).reason == "connection"        # same process kept running
    d = U.classify(500, {"at": 600, "kind": "clean"}, started, 5000)
    assert d.reason == "clean" and d.seconds == 4500
    assert U.classify(500, {"at": 400, "kind": "clean"}, started, 5000).reason == "unknown"   # stale record
    assert U.classify(500, None, started, 5000).reason == "unknown"            # crash / power loss / machine off


def test_downtime_render():
    e = {"ts": NOW.isoformat(), "type": "bot_downtime", "category": "bot",
         "details": json.dumps({"periods": [{"from": 1000, "to": 3820, "seconds": 2820, "reason": "unknown"}]})}
    line = owner_log_line(e)
    assert "offline for **47 min**" in line and "<t:1000:f>" in line and "machine was off" in line
    assert category_of("bot_downtime") == "bot" and category_of("automod_delete") == "moderation"
    assert U.human_duration(2820) == "47 min" and U.human_duration(3 * 3600) == "3 h" and U.human_duration(90000) == "1 d 1 h"


def test_guild_added_render():
    e = {"ts": NOW.isoformat(), "type": "bot_guild_join", "category": "bot",
         "details": json.dumps({"name": "Other place", "members": 12, "while_offline": True})}
    assert "added to server **Other place** · 12 members (while it was offline)" in owner_log_line(e)


# ---------------------------------------------------------------- scheduled backups
def test_backup_schedule():
    now = datetime(2026, 10, 7, 4, 5)
    assert U.backup_due(None, now, 4)
    assert U.backup_due(datetime(2026, 10, 6, 4, 0), now, 4)            # 04:xx and not yet today
    assert not U.backup_due(datetime(2026, 10, 7, 4, 1), now, 4)        # already done today
    assert not U.backup_due(datetime(2026, 10, 6, 23, 0), datetime(2026, 10, 7, 13, 0), 4)
    assert U.backup_due(datetime(2026, 10, 5, 3, 0), datetime(2026, 10, 7, 13, 0), 4)   # catch-up after downtime
    assert U.next_backup(datetime(2026, 10, 7, 4, 1), datetime(2026, 10, 7, 13, 0), 4) == datetime(2026, 10, 8, 4, 0)


def test_snapshot_and_prune(tmp_path):
    src = tmp_path / "live.db"
    live = sqlite3.connect(src)
    live.execute("PRAGMA journal_mode=WAL")            # like the bot's live database
    live.execute("create table t(x)")
    live.execute("insert into t values (42)")
    live.commit()                                     # kept open while backing up, like the running bot
    folder = tmp_path / "backups"
    for i in range(9):
        U.snapshot_sqlite(src, folder / U.backup_name(datetime(2026, 10, 1 + i, 4, 0)))
    (folder / "snapshot-1.json").write_text("{}")                       # other files are never touched
    assert len(U.list_backups(folder)) == 9
    assert sorted(p.name for p in folder.iterdir() if not p.name.startswith("auto-") or "partial" in p.name
                  or p.suffix != ".db") == ["snapshot-1.json"]       # no -wal/-shm/.partial leftovers
    (folder / "auto-x.partial-wal").write_text("")
    assert U.clean_leftovers(folder) == 1
    removed = U.prune(folder, 7)
    assert len(removed) == 2 and len(U.list_backups(folder)) == 7 and (folder / "snapshot-1.json").exists()
    newest = U.list_backups(folder)[0]
    assert U.backup_time(newest) == datetime(2026, 10, 9, 4, 0)
    c = sqlite3.connect(newest)
    assert c.execute("select x from t").fetchone()[0] == 42
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    c.close()
    live.close()


async def test_message_text_retention():
    d = Database(":memory:")
    await d.open()
    old = (NOW - timedelta(days=30)).isoformat(timespec="seconds")
    await d.add_event(type="message_delete", category="message", guild_id=1, ts=old,
                      details={"content": "secret", "message_id": 5})
    await d.add_event(type="message_delete", category="message", guild_id=1, details={"content": "recent"})
    assert await d.strip_message_content(7) == 1
    rows = [dict(r) async for r in await d.conn.execute("select details from events order by id")]
    assert json.loads(rows[0]["details"]) == {"message_id": 5}                 # metadata kept, text gone
    assert json.loads(rows[1]["details"])["content"] == "recent"
    await d.close()


def test_message_logging_is_off_by_default():
    assert ServerConfig().message_logging.metadata is False and ServerConfig().message_logging.content is False
    assert ServerConfig().system.backups_enabled and ServerConfig().system.backup_keep == 7


# ---------------------------------------------------------------- AutoMod
def test_automod_word_matching():
    words = ["bad*", "ass", "سگ"]
    assert blocked_word("this is BADLY done", words) == "bad*"
    assert blocked_word("first class", words) is None                        # whole words only
    assert blocked_word("you ass!", words) == "ass"
    assert blocked_word("naïve", ["naive"]) == "naive"                   # accents folded
    assert blocked_word("ﾌﾞ", []) is None


def test_automod_arabic_script_normalization():
    assert normalize("كيك") == normalize("کیک")                              # Arabic-script letter variants
    assert blocked_word("این يك متن است", ["یک"]) == "یک"
    assert blocked_word("سـگ", ["سگ"]) == "سگ"                                # tatweel
    assert blocked_word("می‌روم", ["میروم"]) == "میروم"                  # zero-width non-joiner


def test_invite_detection_and_word_parsing():
    assert invite_codes("join discord.gg/abc-1 or https://discord.com/invite/XyZ") == ["abc-1", "XyZ"]
    assert invite_codes("no links here https://example.com/invite/abc") == []
    assert parse_words("a, b\nB\n\n c*") == ["a", "b", "c*"]


# ---------------------------------------------------------------- feature registry
def _bot(cogs, **cfg):
    c = ServerConfig.model_validate(cfg)
    return SimpleNamespace(cfg=c, settings=SimpleNamespace(lavalink_uri="http://x", ai_base_url=None),
                           get_cog=lambda n: cogs.get(n), module_status={"music": "failed: ImportError"})


def test_help_is_permission_aware():
    bot = _bot({"Music": 1, "VoiceRooms": 1, "Guests": 1, "Fun": 1, "Tier": 1, "OwnerLogs": 1, "Guardian": 1,
                "AutoMod": SimpleNamespace(cfg={"enabled": False}), "System": 1, "AI": 1},
               voice_rooms={"enabled": False})
    member = "\n".join(features.help_lines(bot, owner=False))
    owner = "\n".join(features.help_lines(bot, owner=True))
    assert "Music" in member and "Guardian" not in member and "AutoMod" not in member
    assert "voice room" not in member                                       # disabled -> hidden from members
    assert "Your own voice room** · *off on this server*" in owner and "AutoMod (words & invites)** · *off" in owner
    status = "\n".join(features.status_lines(_bot({})))
    assert "🔴" in status and "not loaded — failed: ImportError" in status


def test_feature_registry_keys_unique():
    keys = [f.key for f in features.FEATURES]
    assert len(keys) == len(set(keys))


# ---------------------------------------------------------------- v1.2.0: watchdog, diagnostics, backup names
def test_watchdog_decisions():
    from vrbot.watchdog import should_exit
    assert should_exit(1000, 990, 500, 990) is None                         # healthy
    assert "event loop stuck" in should_exit(1000, 600, 500, 990)
    assert should_exit(1000, 995, None, 500) is None                        # short disconnect: let discord.py reconnect
    assert "not connected" in should_exit(2000, 1995, None, 500)            # 25 min disconnected
    assert U.REASONS["watchdog"]


def test_diagnostics_never_leak_secrets(tmp_path, monkeypatch):
    from vrbot.diagnostics import report, sanitize
    tok = "M" + "x" * 23 + "." + "y" * 6 + "." + "z" * 30          # token-shaped, built at runtime (fake)
    s = sanitize(f"token {tok} DISCORD_TOKEN=abc user 123456789012345678 someone@example.com "
                 '{"content": "private text", "x": 1} Authorization: Bot xyz')
    for bad in (tok, "abc user", "123456789012345678", "someone@example.com", "private text", "Bot xyz"):
        assert bad not in s, bad
    monkeypatch.setenv("DISCORD_TOKEN", tok)
    (tmp_path / "heartbeat.json").write_text(json.dumps({"ts": 1, "state": "ready", "modules": {"app": "loaded"}}))
    logs = [json.dumps({"ts": "t", "level": "ERROR", "logger": "x", "msg": f"boom {tok}"}),
            json.dumps({"level": "INFO", "msg": "fine"})]
    out = report(tmp_path, tmp_path, logs)
    assert tok not in out and "DISCORD_TOKEN set" in out and "boom <token>" in out and "fine" not in out
    assert "Database: not created yet" in out and "Version: dev" in out


def test_unified_backup_names(tmp_path):
    for n in ("auto-20261001-040000.db", "manual-20261005-120000.db", "auto-20261003-040000.db", "vrbot-old.db"):
        (tmp_path / n).write_text("x")
    assert [p.name for p in U.list_backups(tmp_path)] == ["manual-20261005-120000.db", "auto-20261003-040000.db",
                                                          "auto-20261001-040000.db"]
    U.prune(tmp_path, 1)                                                     # only nightly backups rotate
    assert sorted(p.name for p in U.list_backups(tmp_path)) == ["auto-20261003-040000.db", "manual-20261005-120000.db"]
