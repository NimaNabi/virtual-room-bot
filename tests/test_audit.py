from conftest import BOT, BOTROLE, C1, C2, C3, GID, P

from vrbot.config import ServerConfig
from vrbot.perms import flags as F
from vrbot.perms.audit import (Op, apply_op, audit, check_bot_can_apply, diff_changes, expectations_for,
                                 match_channels, plan_repair)
from vrbot.perms.engine import compute
from vrbot.perms.model import Guild, Member, Overwrite


def codes(fs):
    return {f.code for f in fs}


def by_code(fs, code):
    return [f for f in fs if f.code == code]


def test_detects_trust_level_2_seeing_trust_level_1_voice(guild, cfg):
    fs = audit(guild, cfg, BOT)
    exposed = [f for f in by_code(fs, "channel_access") if "inner-voice" in f.title and f.trust_level == "trust_level_2"]
    assert exposed and exposed[0].severity == "CRITICAL"


def test_detects_trust_level_2_cannot_send_in_gaming(guild, cfg):
    fs = audit(guild, cfg, BOT)
    f = [f for f in by_code(fs, "channel_access") if "#gaming" in f.title and f.trust_level == "trust_level_2"]
    assert f and "cannot Send Messages" in f[0].title
    assert f[0].fixable


def test_trust_level_3_read_only_expectation_flags_send(guild, cfg):
    fs = audit(guild, cfg, BOT)
    f = [f for f in by_code(fs, "channel_access") if "#gaming" in f.title and f.trust_level == "trust_level_3"]
    assert f and "should not" in f[0].title


def test_trust_level_extra_dangerous_perm(guild, cfg):
    guild.roles[C3].permissions |= P("manage_channels")
    fs = audit(guild, cfg, BOT)
    f = by_code(fs, "trust_level_extra_perm")
    assert f and "Manage Channels" in f[0].title and f[0].severity == "WARNING"
    guild.roles[C3].permissions |= P("administrator")
    f = by_code(audit(guild, cfg, BOT), "trust_level_extra_perm")
    assert f[0].severity == "CRITICAL"


def test_allow_dangerous_is_respected(guild, cfg):
    guild.roles[C1].permissions |= P("move_members")
    cfg.baseline.trust_levels["trust_level_1"].allow_dangerous = ["move_members"]
    assert not by_code(audit(guild, cfg, BOT), "trust_level_extra_perm")


def test_trust_level_missing_required_perm(guild, cfg):
    cfg.baseline.trust_levels["trust_level_1"].require = ["stream"]
    f = by_code(audit(guild, cfg, BOT), "trust_level_missing_perm")
    assert f and f[0].ops[0].kind == "role_add"


def test_unexpected_admin_role_and_member(guild, cfg):
    guild.roles[C2].permissions |= P("administrator")
    fs = audit(guild, cfg, BOT)
    # a Level role with Administrator: reported exactly once, as a CRITICAL trust_level violation with holders
    f = [f for f in fs if "Level 2" in f.title and "Administrator" in f.title]
    assert len(f) == 1 and f[0].code == "trust_level_extra_perm" and f[0].severity == "CRITICAL" and "TestUserB" in f[0].detail
    from vrbot.perms.model import Role
    guild.roles[99] = Role(99, "Helper", P("administrator"), 1)
    guild.members[52].role_ids.append(99)
    f = [f for f in audit(guild, cfg, BOT) if f.code == "unexpected_admin_role"]
    assert f and "Ali" in f[0].detail


def test_everyone_dangerous(guild, cfg):
    guild.roles[GID].permissions |= P("ban_members")
    f = by_code(audit(guild, cfg, BOT), "everyone_dangerous")
    assert f and f[0].severity == "CRITICAL"


def test_trust_level_order(guild, cfg):
    guild.roles[C3].position, guild.roles[C1].position = 3, 1
    assert by_code(audit(guild, cfg, BOT), "trust_level_order")


def test_bot_below_trust_level_is_critical(guild, cfg):
    guild.roles[BOTROLE].position = 2
    f = by_code(audit(guild, cfg, BOT), "bot_below_trust_level")
    assert f and all(x.severity == "CRITICAL" for x in f)


def test_bot_missing_permissions(guild, cfg):
    guild.roles[BOTROLE].permissions &= ~P("view_audit_log")
    f = by_code(audit(guild, cfg, BOT), "bot_missing_perm")
    assert f and "View Audit Log" in f[0].title


def test_bot_cannot_view_private_channel(guild, cfg):
    assert any(f.code == "bot_cannot_view" and "inner-trust_level" in f.title for f in audit(guild, cfg, BOT))


def test_member_overwrite_and_orphans(guild, cfg):
    guild.channels[100].overwrites[52] = Overwrite(52, "member", allow=P("manage_messages"))
    guild.channels[100].overwrites[999] = Overwrite(999, "role", deny=P("send_messages"))
    fs = audit(guild, cfg, BOT)
    assert by_code(fs, "member_overwrite")
    assert by_code(fs, "orphan_overwrite")


def test_member_overprivileged(guild, cfg):
    guild.roles[99] = __import__("vrbot.perms.model", fromlist=["Role"]).Role(99, "Helper", P("manage_roles"), 1)
    guild.members[52].role_ids.append(99)
    f = by_code(audit(guild, cfg, BOT), "member_overprivileged")
    assert f and "Ali" in f[0].title and "Manage Roles" in f[0].title


def test_conflicting_overwrites(guild, cfg):
    guild.channels[300].overwrites[C1] = Overwrite(C1, "role", allow=P("send_messages"))
    assert by_code(audit(guild, cfg, BOT), "conflicting_overwrite")


def test_hierarchy_order(guild, cfg):
    guild.roles[11].position = 1  # Moderator below trust_levels
    assert by_code(audit(guild, cfg, BOT), "hierarchy_order")


def test_missing_trust_level_role(guild, cfg):
    cfg.baseline.trust_levels["trust_level_1"].role = "Nope"
    assert by_code(audit(guild, cfg, BOT), "trust_level_role_missing")


def test_match_channels_category_includes_children(guild):
    from vrbot.config import ChannelRule
    got = {c.id for c in match_channels(guild, ChannelRule(match="category:Level 1", access={}))}
    assert got == {200, 201, 202}


def test_expectations_filter_by_channel_type(guild, cfg):
    exp = expectations_for(guild, cfg.baseline)
    assert "connect" not in exp[201]["trust_level_1"]           # text channel
    assert "send_messages" not in exp.get(202, {}).get("trust_level_1", {})


def test_severity_sorting(guild, cfg):
    fs = audit(guild, cfg, BOT)
    sev = [f.severity for f in fs]
    assert sev == sorted(sev, key=lambda s: {"CRITICAL": 0, "WARNING": 1, "INFO": 2}[s])


# ---------------------------------------------------------------- repair plans
def test_plan_repair_fixes_channel_drift_without_new_problems(guild, cfg):
    guild.roles[BOTROLE].permissions |= P("administrator")  # let bot apply everything
    plan = plan_repair(guild, cfg, BOT)
    assert plan.changes
    assert not plan.introduced
    assert not plan.blocked
    remaining = {f.key for f in plan.remaining if f.code == "channel_access"}
    assert not remaining


def test_plan_repair_gaming_removes_deny_for_trust_level_2(guild, cfg):
    guild.roles[BOTROLE].permissions |= P("administrator")
    plan = plan_repair(guild, cfg, BOT, scope="trust_level_2")
    gaming = [c for c in plan.changes if c.channel_id == 300 and c.target_id == C2]
    assert gaming
    # deny was only on send_messages -> overwrite becomes empty -> removed (minimal change)
    assert gaming[0].after is None or not (gaming[0].after[1] & P("send_messages"))


def test_plan_repair_trust_level_3_readonly_gets_deny(guild, cfg):
    guild.roles[BOTROLE].permissions |= P("administrator")
    plan = plan_repair(guild, cfg, BOT, scope="trust_level_3")
    c = [c for c in plan.changes if c.channel_id == 300 and c.target_id == C3][0]
    assert c.after[1] & P("send_messages")


def test_plan_verified_by_simulation(guild, cfg):
    guild.roles[BOTROLE].permissions |= P("administrator")
    plan = plan_repair(guild, cfg, BOT)
    sim = guild.clone()
    for c in plan.changes:
        if c.kind == "overwrite":
            ch = sim.channels[c.channel_id]
            if c.after is None:
                ch.overwrites.pop(c.target_id, None)
            else:
                ch.overwrites[c.target_id] = Overwrite(c.target_id, c.target_type, *c.after)
        else:
            sim.roles[c.target_id].permissions = c.after
    s2 = Member(-5, "x", [C2])
    assert compute(sim, s2, sim.channels[300]).value & F.FLAGS["send_messages"]
    assert not compute(sim, s2, sim.channels[202]).value & F.FLAGS["view_channel"]


def test_plan_blocked_when_bot_lacks_rights(guild, cfg):
    guild.roles[BOTROLE].position = 0
    guild.roles[BOTROLE].permissions &= ~P("manage_roles")
    cfg.baseline.trust_levels["trust_level_1"].require = ["stream"]
    plan = plan_repair(guild, cfg, BOT, scope="trust_level_1")
    assert plan.blocked


def test_bot_cannot_grant_perms_it_lacks(guild):
    after = guild.clone()
    after.roles[C1].permissions |= P("mention_everyone")
    blocked = check_bot_can_apply(guild, BOT, diff_changes(guild, after))
    assert any("cannot grant" in b for b in blocked)


def test_plan_empty_when_compliant(cfg):
    g = Guild(id=1, name="x", owner_id=9)
    g.roles[1] = __import__("vrbot.perms.model", fromlist=["Role"]).Role(1, "@everyone", P("view_channel"), 0)
    plan = plan_repair(g, ServerConfig(), None)
    assert not plan.changes


def test_apply_op_removes_empty_overwrite(guild):
    apply_op(guild, Op("ow_neutral", P("send_messages"), channel_id=300, target_id=C2))
    assert C2 not in guild.channels[300].overwrites


def test_change_describe(guild):
    after = guild.clone()
    after.roles[C1].permissions |= P("stream")
    after.channels[300].overwrites.pop(C2)
    ds = [c.describe() for c in diff_changes(guild, after)]
    assert any("+Stream" in d for d in ds)
    assert any("removed" in d for d in ds)


def test_match_channels_category_id_includes_children(guild):
    # regression: an id: rule on a category must protect the channels inside it (auto-heal missed drift)
    from vrbot.config import ChannelRule
    assert {c.id for c in match_channels(guild, ChannelRule(match="id:200", access={}))} == {200, 201, 202}
    assert {c.id for c in match_channels(guild, ChannelRule(match="id:201", access={}))} == {201}
