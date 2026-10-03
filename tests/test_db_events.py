from datetime import datetime, timedelta, timezone

import pytest

from vrbot.authz import Level, level_for, moderation_block
from vrbot.config import ChannelRule, ServerConfig
from vrbot.db import Database, LogQuery, iso, parse_duration
from vrbot.events import (AuditBuffer, AuditRec, JoinRate, VoiceAttributor, bot_reason, category_of, diff_member,
                            diff_voice, parse_bot_reason)
from vrbot.perms.model import Guild
from vrbot.perms.snapshot import restore_changes, structural_diff
from vrbot.render import event_dict_for_ai, event_line

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------- database
@pytest.fixture
async def db():
    d = Database(":memory:")
    await d.open()
    yield d
    await d.close()


async def test_migrations_idempotent(db):
    assert await db.migrate() == []


async def test_event_roundtrip_and_filters(db):
    await db.add_event(type="member_ban", category="moderation", guild_id=1, target_id=5, target_name="Y",
                       actor_id=9, actor_name="TestOwner", actor_confidence="confirmed", reason="spam")
    await db.add_event(type="voice_move", category="voice", guild_id=1, target_id=5, target_name="Y",
                       actor_id=8, actor_name="Mod", actor_confidence="likely", details={"from_name": "a", "to_name": "b"})
    await db.add_event(type="member_join", category="membership", guild_id=1, target_id=6, target_name="Z")
    assert len(await db.query_events(LogQuery(guild_id=1))) == 3
    assert [e["type"] for e in await db.query_events(LogQuery(user_id=5))] == ["voice_move", "member_ban"]
    bans = await db.query_events(LogQuery(types=["member_ban"]))
    assert bans[0]["actor_name"] == "TestOwner" and bans[0]["reason"] == "spam"
    assert len(await db.query_events(LogQuery(actor_id=8))) == 1
    assert len(await db.query_events(LogQuery(text="spam"))) == 1
    assert len(await db.query_events(LogQuery(category="voice"))) == 1
    assert len(await db.query_events(LogQuery(limit=1))) == 1


async def test_since_filter_and_prune(db):
    old = iso(datetime.now(timezone.utc) - timedelta(days=40))
    await db.add_event(type="message_delete", category="message", guild_id=1, ts=old)
    await db.add_event(type="member_join", category="membership", guild_id=1, ts=old)
    await db.add_event(type="member_join", category="membership", guild_id=1)
    since = iso(datetime.now(timezone.utc) - timedelta(days=7))
    assert len(await db.query_events(LogQuery(since=since))) == 1
    removed = await db.prune(events_days=365, message_days=30, voice_days=180)
    assert removed == 1  # only the old message event
    assert (await db.count_events(old))["member_join"] == 2


async def test_cases_snapshots_batches_kv(db):
    cid = await db.add_case(guild_id=1, action="warn", moderator_id=9, moderator_name="TestOwner", user_id=5, reason="x")
    assert (await db.cases_for(1, 5))[0]["id"] == cid
    assert await db.deactivate_case(cid)
    sid = await db.add_snapshot(1, "manual", {"id": 1, "name": "g", "owner_id": 2, "roles": [], "channels": []})
    await db.add_snapshot(1, "auto", {"id": 1, "name": "g", "owner_id": 2, "roles": [], "channels": []})
    assert (await db.get_snapshot(sid))["data"]["name"] == "g"
    assert len(await db.list_snapshots(1)) == 2
    assert (await db.latest_snapshot(1))["kind"] == "auto"
    await db.prune_snapshots(1, keep=0)
    assert [s["kind"] for s in await db.list_snapshots(1)] == ["manual"]
    bid = await db.add_batch(1, 9, "repair", "planned", "s", [{"kind": "role_perms"}])
    await db.update_batch(bid, "applied", {"verified": 1})
    assert (await db.get_batch(bid))["status"] == "applied"
    await db.kv_set("k", {"a": 1})
    assert await db.kv_get("k") == {"a": 1}
    assert (await db.stats())["events"] == 0


def test_parse_duration():
    assert parse_duration("7d") == timedelta(days=7)
    assert parse_duration("1d12h") == timedelta(days=1, hours=12)
    assert parse_duration("30m") == timedelta(minutes=30)
    with pytest.raises(ValueError):
        parse_duration("soon")
    with pytest.raises(ValueError):
        parse_duration("7x")


def test_query_builder_parameterized():
    sql, params = LogQuery(text="'; DROP TABLE events; --", limit=10).build()
    assert "DROP" not in sql.upper() and any("drop table" in str(p) for p in params)


# ---------------------------------------------------------------- normalization
def test_diff_voice():
    a = {"channel_id": None}
    b = {"channel_id": 1, "channel_name": "General", "mute": False, "deaf": False}
    c = {"channel_id": 2, "channel_name": "Gaming", "mute": False, "deaf": False}
    assert [t for t, _ in diff_voice(a, b)] == ["voice_join"]
    assert [t for t, _ in diff_voice(b, a)] == ["voice_leave"]
    ev = diff_voice(b, c)
    assert ev[0][0] == "voice_move" and ev[0][1]["to_name"] == "Gaming"
    assert [t for t, _ in diff_voice(b, {**b, "mute": True})] == ["voice_server_mute"]
    assert [t for t, _ in diff_voice(b, {**b, "deaf": True})] == ["voice_server_deafen"]
    assert diff_voice(a, {**b, "mute": True}) == [("voice_join", {"to": 1, "to_name": "General"})]


def test_diff_member():
    before = {"nick": None, "role_ids": {1}, "role_names": {1: "A", 2: "B"}, "timeout_until": None}
    after = {"nick": "X", "role_ids": {2}, "role_names": {2: "B"}, "timeout_until": "2026-10-01T00:00:00+00:00"}
    types = [t for t, _ in diff_member(before, after)]
    assert types == ["nick_change", "role_add", "role_remove", "timeout_add"]
    assert [t for t, _ in diff_member(after, {**after, "timeout_until": None})] == ["timeout_remove"]


def test_categories():
    assert category_of("member_ban") == "moderation"
    assert category_of("overwrite_update") == "structure"
    assert category_of("voice_move") == "voice"


def test_bot_reason_roundtrip():
    r = bot_reason("TestOwner", 123, "spamming")
    assert parse_bot_reason(r) == (123, "TestOwner", "spamming")
    assert parse_bot_reason("manual reason") is None


def test_audit_buffer_find_and_consume():
    b = AuditBuffer()
    b.add(AuditRec(1, "kick", 9, "Mod", 5, "bye", NOW))
    assert b.find({"ban"}, 5, NOW) is None
    assert b.find({"kick"}, 5, NOW + timedelta(seconds=3)).executor_id == 9
    assert b.find({"kick"}, 5, NOW) is None  # consumed
    b.add(AuditRec(2, "kick", 9, "Mod", 6, None, NOW))
    assert b.find({"kick"}, 6, NOW + timedelta(seconds=60)) is None  # outside window


def test_voice_attributor_new_entry_and_count_bump():
    v = VoiceAttributor()
    v.prime([{"id": 10, "count": 3}])  # historical entry: never credited
    v.observe([{"id": 10, "action": "member_disconnect", "count": 3, "executor_id": 9, "created_at": NOW}], NOW)
    assert v.match("member_disconnect", None, NOW)[2] == "unknown"
    # same moderator disconnects again -> Discord bumps count on the aggregated entry
    v.observe([{"id": 10, "action": "member_disconnect", "count": 4, "executor_id": 9, "created_at": NOW}], NOW)
    ex, _, conf = v.match("member_disconnect", None, NOW)
    assert ex == 9 and conf == "likely"
    assert v.match("member_disconnect", None, NOW)[2] == "unknown"  # credit consumed


def test_voice_attributor_move_matches_destination():
    v = VoiceAttributor()
    v.observe([{"id": 11, "action": "member_move", "count": 2, "executor_id": 7, "channel_id": 222,
                "created_at": NOW}], NOW)
    assert v.match("member_move", 333, NOW)[2] == "unknown"
    assert v.match("member_move", 222, NOW)[0] == 7
    assert v.match("member_move", 222, NOW)[0] == 7   # count 2 => two moves
    assert v.match("member_move", 222, NOW)[2] == "unknown"


def test_voice_attributor_ambiguous():
    v = VoiceAttributor()
    v.observe([{"id": 1, "action": "member_disconnect", "count": 1, "executor_id": 7, "created_at": NOW},
               {"id": 2, "action": "member_disconnect", "count": 1, "executor_id": 8, "created_at": NOW}], NOW)
    assert v.match("member_disconnect", None, NOW)[2] == "ambiguous"


def test_voice_attributor_ignores_stale_new_entries():
    v = VoiceAttributor(window=45)
    v.observe([{"id": 3, "action": "member_disconnect", "count": 1, "executor_id": 7,
                "created_at": NOW - timedelta(minutes=10)}], NOW)
    assert v.match("member_disconnect", None, NOW)[2] == "unknown"


def test_join_rate():
    r = JoinRate(3, 60)
    assert not r.add(NOW)
    assert not r.add(NOW + timedelta(seconds=10))
    assert r.add(NOW + timedelta(seconds=20))
    assert not r.add(NOW + timedelta(seconds=25))  # already alerted for this burst
    assert not r.add(NOW + timedelta(seconds=300))


# ---------------------------------------------------------------- authz
def test_levels():
    base = dict(guild_owner_id=1, owner_ids={2}, admin_role_ids={10}, mod_role_ids={11}, trusted_role_ids={21},
                has_administrator=False, has_mod_perms=False)
    assert level_for(user_id=1, role_ids=set(), **base) == Level.OWNER
    assert level_for(user_id=2, role_ids=set(), **base) == Level.OWNER
    assert level_for(user_id=3, role_ids={10}, **base) == Level.ADMIN
    assert level_for(user_id=3, role_ids=set(), **{**base, "has_administrator": True}) == Level.ADMIN
    assert level_for(user_id=3, role_ids={11}, **base) == Level.MOD
    assert level_for(user_id=3, role_ids={21}, **base) == Level.TRUSTED
    assert level_for(user_id=3, role_ids={99}, **base) == Level.MEMBER


def test_moderation_block_rules():
    kw = dict(actor_id=5, actor_top=5, actor_level=Level.MOD, target_id=6, target_top=3, target_level=Level.MEMBER,
              bot_top=8, guild_owner_id=1, bot_id=2)
    assert moderation_block(**kw) is None
    assert "yourself" in moderation_block(**{**kw, "target_id": 5})
    assert "owner" in moderation_block(**{**kw, "target_id": 1})
    assert "the bot" in moderation_block(**{**kw, "bot_top": 3})
    assert "yours" in moderation_block(**{**kw, "target_top": 5})
    assert moderation_block(**{**kw, "actor_id": 1, "actor_top": 1, "target_top": 5}) is None  # owner overrides
    assert "itself" in moderation_block(**{**kw, "target_id": 2})


# ---------------------------------------------------------------- config
def test_config_parsing_and_levels():
    cfg = ServerConfig.model_validate({"baseline": {
        "trust_levels": {"trust_level_1": {"role": "C1", "rank": 1, "require": ["send", "View Channel"]}},
        "channels": [{"match": "#a", "access": {"trust_level_1": "voice", "default": {"view": False}}}]}})
    assert cfg.baseline.trust_levels["trust_level_1"].require == ["send_messages", "view_channel"]
    r = cfg.baseline.channels[0]
    assert r.expectations("trust_level_1") == {"view_channel": True, "connect": True, "speak": True}
    assert r.expectations("default") == {"view_channel": False}
    assert r.expectations("trust_level_2") is None


def test_config_rejects_bad_values():
    with pytest.raises(Exception):
        ServerConfig.model_validate({"baseline": {"trust_levels": {"c": {"role": "x", "rank": 1, "require": ["fly"]}}}})
    with pytest.raises(ValueError):
        ChannelRule(match="#a", access={"c": "superuser"}).expectations("c")


def test_default_config_template_is_generic():
    from pathlib import Path

    import yaml

    from vrbot.config import load_server_config
    path = Path(__file__).parent.parent / "config" / "default.yaml"
    cfg = load_server_config(path)
    assert set(cfg.baseline.trust_levels) == {"trust_level_1", "trust_level_2", "trust_level_3"}
    assert cfg.privacy.tiers["owner_area"].rank == 0 and cfg.privacy.areas == {}
    assert cfg.privacy.owner_id is None and cfg.privacy.approved_owner_equivalents == []
    assert not cfg.autoheal.enabled and cfg.message_logging.content is False
    import re
    assert not re.search(r"\d{17,20}", path.read_text(encoding="utf-8"))   # no server-specific IDs in the template
    assert yaml.safe_load(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- snapshots / render / cli
def test_snapshot_roundtrip_diff_restore(guild):
    d = guild.to_dict()
    g2 = Guild.from_dict(d)
    assert g2.to_dict() == d
    assert structural_diff(guild, g2) == []
    g2.roles[22].permissions |= 1 << 4
    g2.channels[300].overwrites.pop(22)
    g2.channels.pop(101)
    lines = structural_diff(guild, g2)
    assert any("Level 2 permissions" in line for line in lines)
    assert any("overwrite removed" in line for line in lines)
    assert any("announcements deleted" in line for line in lines)
    changes, notes = restore_changes(g2, guild)
    assert {c.kind for c in changes} == {"role_perms", "overwrite"}
    assert any("announcements" in n for n in notes)


def test_event_line_rendering():
    e = {"ts": "2026-09-30T10:00:00+00:00", "type": "voice_move", "target_name": "TestUserB", "actor_name": "Mod",
         "actor_confidence": "likely", "details": '{"from_name": "General", "to_name": "AFK"}'}
    s = event_line(e, discord_ts=False)
    assert "TestUserB" in s and "#General → #AFK" in s and "likely" in s
    e2 = {"ts": "x", "type": "voice_leave", "target_name": "TestUserB", "actor_confidence": "self", "channel_name": "General"}
    assert "by" not in event_line(e2, discord_ts=False)
    e3 = {"ts": "x", "type": "member_ban", "target_name": "Y", "actor_confidence": "unknown"}
    assert "by unknown" in event_line(e3, discord_ts=False)


def test_ai_view_strips_message_text():
    e = {"ts": "t", "type": "message_edit", "details": '{"before": "secret", "after": "x", "message_id": 1}'}
    d = event_dict_for_ai(e)
    assert "before" not in d["details"] and d["details"]["message_id"] == 1


def test_invite_permissions():
    from vrbot.cli import INVITE_PERMS, client_id_from_token, invite_url, permissions_integer
    from vrbot.perms import flags as F
    v = permissions_integer()
    assert not v & F.FLAGS["administrator"]
    assert v & F.FLAGS["view_audit_log"] and v & F.FLAGS["manage_roles"]
    assert "administrator" not in INVITE_PERMS
    import base64
    tok = base64.b64encode(b"123456789012345678").decode().rstrip("=") + ".abc.def"
    assert client_id_from_token(tok) == "123456789012345678"
    assert "client_id=123" in invite_url("123") and "applications.commands" in invite_url("123")
