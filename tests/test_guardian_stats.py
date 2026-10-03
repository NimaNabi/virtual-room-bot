from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from conftest import BOT, BOTROLE, C2, GID, P

from vrbot.bot import MutationRefused, ServerBot
from vrbot.config import Settings
from vrbot.guardian_rules import CRITICAL, INFO, WARNING, Burst, Ctx, classify, health_score
from vrbot.perms.audit import Finding, audit
from vrbot.perms.model import Member, Role
from vrbot.stats import guardian_counts, per_day, voice_sessions, voice_summary

T0 = datetime(2026, 9, 30, 20, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------- guardian classification
def test_admin_grant_is_critical():
    assert classify("role_update", {"permissions": {"added": ["administrator"], "removed": []}}, Ctx(actor="X", target="2"))[0] == CRITICAL
    assert classify("role_create", {"permissions": {"added": ["administrator"]}}, Ctx())[0] == CRITICAL


def test_dangerous_grant_is_warning_and_plain_change_info():
    assert classify("role_update", {"permissions": {"added": ["ban_members"]}}, Ctx())[0] == WARNING
    assert classify("role_update", {"color": {"before": "1", "after": "2"}}, Ctx())[0] == INFO


def test_trust_level_role_losing_perms_warns():
    sev, title = classify("role_update", {"permissions": {"added": [], "removed": ["send_messages"]}}, Ctx(target="1", target_is_trust_level_role=True))
    assert sev == WARNING and "lost" in title


def test_private_channel_exposed_is_critical():
    ctx = Ctx(target="secret", target_is_everyone=True)
    assert classify("overwrite_update", {"deny": {"added": [], "removed": ["view_channel"]}}, ctx)[0] == CRITICAL
    assert classify("overwrite_update", {"allow": {"added": ["view_channel"], "removed": []}}, ctx)[0] == CRITICAL
    assert classify("overwrite_update", {"deny": {"added": ["view_channel"], "removed": []}}, ctx)[0] == INFO


def test_member_overwrite_and_trust_level_loses_view():
    assert classify("overwrite_create", {}, Ctx(target_is_member=True))[0] == WARNING
    assert classify("overwrite_update", {"deny": {"added": ["view_channel"]}}, Ctx(target_is_trust_level_role=True))[0] == WARNING


def test_bots_and_integrations():
    assert classify("bot_add", {}, Ctx(target="Evil"))[0] == CRITICAL
    assert classify("bot_add", {}, Ctx(target="Jockie Music", trusted_bot=True))[0] == INFO
    assert classify("webhook_create", {}, Ctx())[0] == WARNING
    assert classify("member_prune", {}, Ctx())[0] == CRITICAL


def test_role_assignment_alerts_only_when_privileged():
    assert classify("member_role_update", {}, Ctx()) is None
    assert classify("member_role_update", {}, Ctx(role_grants_admin=True))[0] == CRITICAL
    assert classify("member_role_update", {}, Ctx(role_grants_dangerous=["kick_members"]))[0] == WARNING


def test_role_delete_severity():
    assert classify("role_delete", {}, Ctx(target_is_trust_level_role=True))[0] == CRITICAL
    assert classify("role_delete", {}, Ctx())[0] == WARNING


def test_burst_detection_fires_once_per_window():
    b = Burst(3, 300)
    k = ("ban", 7)
    assert not b.add(k, T0)
    assert not b.add(k, T0 + timedelta(seconds=10))
    assert b.add(k, T0 + timedelta(seconds=20))
    assert not b.add(k, T0 + timedelta(seconds=30))
    assert not b.add(("ban", 8), T0 + timedelta(seconds=30))


def test_health_score_is_derived_from_findings():
    f = lambda s: Finding(s, "x", "t")  # noqa: E731
    assert health_score([]) == 100
    assert health_score([f(CRITICAL)]) == 80
    assert health_score([f(WARNING)] * 2 + [f(INFO)] * 4) == 88
    assert health_score([f(INFO)] * 100) == 90  # INFO capped
    assert health_score([f(CRITICAL)] * 10) == 0


# ---------------------------------------------------------------- new audit findings
def test_everyone_missing_basics(guild, cfg):
    guild.roles[GID].permissions &= ~P("read_message_history")
    cfg.baseline.default.require = ["read_message_history", "use_application_commands"]
    f = [x for x in audit(guild, cfg, BOT) if x.code == "everyone_missing_perm"]
    assert f and "Use Application Commands" in f[0].title and "#rules" in f[0].detail
    assert f[0].ops[0].kind == "role_add"


def test_third_party_bot_admin(guild, cfg):
    guild.roles[77] = Role(77, "Jockie Music", P("administrator"), 1, managed=True)
    guild.members[70] = Member(70, "Jockie Music", [77], bot=True)
    f = [x for x in audit(guild, cfg, BOT) if x.code == "third_party_bot_admin"]
    assert f and f[0].severity == CRITICAL


def test_bot_admin_intended_not_reported(guild, cfg):
    guild.roles[BOTROLE].permissions |= P("administrator")
    assert not [x for x in audit(guild, cfg, BOT) if x.code == "bot_is_admin"]
    cfg.security.bot_admin_intended = False
    assert [x for x in audit(guild, cfg, BOT) if x.code == "bot_is_admin"]


def test_trust_level_extra_not_double_reported_per_member(guild, cfg):
    guild.roles[C2].permissions |= P("move_members")
    fs = audit(guild, cfg, BOT)
    assert [x for x in fs if x.code == "trust_level_extra_perm"]
    assert not [x for x in fs if x.code == "member_overprivileged" and "TestUserB" in x.title]


# ---------------------------------------------------------------- safe mode guard
def _bot(tmp: Path) -> ServerBot:
    return ServerBot(Settings(token=None, guild_id=None, owner_ids=[], data_dir=tmp, config_path=tmp / "server.yaml",
                            log_level="INFO", lavalink_uri=None, lavalink_password=None, ai_base_url=None, ai_api_key=None,
                            ai_model="x", ai_fallback_models=[], message_content_intent=False))


def test_safe_mode_file_blocks_every_mutation(tmp_path):
    b = _bot(tmp_path)
    b.guard("kick", 1)
    (tmp_path / "SAFE_MODE").write_text("on")
    for kind in ("kick", "ban", "permission_batch", "channel_create", "voice_room", "other"):
        with pytest.raises(MutationRefused, match="SAFE MODE"):
            b.guard(kind, 1)


def test_mod_rate_limit_and_batch_limit(tmp_path):
    b = _bot(tmp_path)
    for _ in range(b.cfg.security.max_mod_actions_per_10min):
        b.guard("ban", 42)
    with pytest.raises(MutationRefused, match="Rate limit"):
        b.guard("kick", 42)
    b.guard("kick", 43)  # other moderators unaffected
    with pytest.raises(MutationRefused, match="safety limit"):
        b.guard("permission_batch", 1, count=b.cfg.security.max_changes_per_batch + 1)


# ---------------------------------------------------------------- stats
def ev(i, t, uid, minutes, **kw):
    return {"id": i, "type": t, "target_id": uid, "target_name": f"u{uid}", "category": "voice",
            "ts": (T0 + timedelta(minutes=minutes)).isoformat(), **kw}


def test_voice_sessions_join_move_leave():
    events = [ev(1, "voice_join", 1, 0, channel_name="General"),
              ev(2, "voice_move", 1, 30, details='{"from_name": "General", "to_name": "Gaming"}'),
              ev(3, "voice_leave", 1, 90, channel_name="Gaming"),
              ev(4, "voice_join", 2, 10, channel_name="Gaming")]
    s = voice_sessions(events, now=T0 + timedelta(minutes=70))
    by = {(x.user_id, x.channel): x for x in s}
    assert by[(1, "General")].seconds == 1800
    assert by[(1, "Gaming")].seconds == 3600
    assert by[(2, "Gaming")].ongoing and by[(2, "Gaming")].seconds == 3600
    summ = voice_summary(s)
    assert summ["sessions"] == 3 and summ["unique_users"] == 2
    assert summ["top_channels"][0][0] == "Gaming"


def test_voice_sessions_cap_open_sessions():
    s = voice_sessions([ev(1, "voice_join", 1, 0, channel_name="A")], now=T0 + timedelta(days=3), max_hours=12)
    assert s[0].seconds == 12 * 3600 and not s[0].ongoing


def test_leave_without_join_ignored_and_mod_events_without_target():
    s = voice_sessions([ev(1, "voice_leave", 1, 5), {"id": 2, "type": "voice_mod_move", "target_id": None, "ts": T0.isoformat()}])
    assert s == []


def test_per_day_and_guardian_counts():
    rows = [{"type": "member_join", "ts": "2026-09-01T10:00:00+00:00"}, {"type": "member_join", "ts": "2026-09-01T11:00:00+00:00"},
            {"type": "member_join", "ts": "2026-09-02T10:00:00+00:00"},
            {"type": "guardian_alert", "ts": "2026-09-02T10:00:00+00:00", "details": '{"severity": "CRITICAL"}'}]
    assert per_day(rows, {"member_join"}) == [("2026-09-01", 2), ("2026-09-02", 1)]
    assert guardian_counts(rows)["CRITICAL"] == 1


def test_owner_ids_not_shadowed_by_discord_py(tmp_path):
    # regression: commands.Bot defines an `owner_ids` attribute that would shadow our method
    b = _bot(tmp_path)
    assert isinstance(b.bot_owner_ids(), set)
    b.settings.owner_ids = [5]
    assert 5 in b.bot_owner_ids()
