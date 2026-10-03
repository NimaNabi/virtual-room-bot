from conftest import C1, C2, C3, GID, MOD_ROLE, OWNER, P

from vrbot.perms import flags as F
from vrbot.perms.engine import compute, explain
from vrbot.perms.model import Member, Overwrite


def can(g, member_id, ch_id, perm):
    return bool(compute(g, g.members[member_id], g.channels[ch_id]).value & F.FLAGS[perm])


def test_everyone_base_permissions(guild):
    assert can(guild, 50, 100, "send_messages")
    assert can(guild, 50, 100, "view_channel")


def test_everyone_overwrite_deny(guild):
    assert not can(guild, 50, 101, "send_messages")
    assert can(guild, 50, 101, "view_channel")


def test_role_deny_beats_base_grant(guild):
    assert not can(guild, 50, 300, "send_messages")  # TestUserB (trust_level_2) denied in #gaming
    assert can(guild, 51, 300, "send_messages")      # trust_level_1 unaffected


def test_role_allow_beats_role_deny_when_member_has_both(guild):
    guild.channels[300].overwrites[C1] = Overwrite(C1, "role", allow=P("send_messages"))
    guild.members[50].role_ids = [C1, C2]
    assert can(guild, 50, 300, "send_messages")


def test_member_overwrite_beats_role_allow(guild):
    guild.channels[100].overwrites[51] = Overwrite(51, "member", deny=P("send_messages"))
    assert not can(guild, 51, 100, "send_messages")
    guild.channels[300].overwrites[50] = Overwrite(50, "member", allow=P("send_messages"))
    assert can(guild, 50, 300, "send_messages")


def test_private_category_visibility(guild):
    assert can(guild, 51, 201, "view_channel")
    assert not can(guild, 52, 201, "view_channel")
    assert not can(guild, 50, 201, "view_channel")


def test_implicit_no_view_means_nothing(guild):
    assert not can(guild, 52, 201, "send_messages")
    assert compute(guild, guild.members[52], guild.channels[201]).value == 0


def test_implicit_send_denied_removes_attach_and_embed(guild):
    v = compute(guild, guild.members[50], guild.channels[300]).value
    assert not v & F.FLAGS["attach_files"] and not v & F.FLAGS["embed_links"]


def test_implicit_voice_connect_denied_removes_speak(guild):
    assert not can(guild, 52, 301, "connect")
    assert not can(guild, 52, 301, "speak")
    assert can(guild, 50, 301, "speak")


def test_admin_bypasses_overwrites(guild):
    guild.members[60] = Member(60, "Admin2", [10])
    assert can(guild, 60, 201, "view_channel")
    assert compute(guild, guild.members[60], guild.channels[201]).value == F.ALL


def test_owner_has_everything(guild):
    assert compute(guild, guild.members[OWNER], guild.channels[201]).owner


def test_timeout_leaves_only_view_and_history(guild):
    guild.members[50].timed_out = True
    v = compute(guild, guild.members[50], guild.channels[100]).value
    assert v == P("view_channel", "read_message_history")


def test_timeout_does_not_affect_admin(guild):
    guild.members[OWNER].timed_out = True
    assert can(guild, OWNER, 100, "send_messages")


def test_explain_role_deny_reason_and_fix(guild):
    ex = explain(guild, guild.members[50], guild.channels[300], ["send_messages"])
    t = ex.traces[0]
    assert not t.allowed
    assert "DENY" in t.reason and "@Level 2" in t.reason
    assert "Remove the DENY" in t.fix and "#gaming" in t.fix


def test_explain_everyone_deny_suggests_allow_for_members_role(guild):
    t = explain(guild, guild.members[52], guild.channels[201], ["view_channel"]).traces[0]
    assert not t.allowed
    assert "@everyone" in t.reason
    assert "ALLOW" in t.fix and "@Level 3" in t.fix


def test_explain_implicit_dependency(guild):
    t = explain(guild, guild.members[52], guild.channels[301], ["speak"]).traces[0]
    assert not t.allowed
    assert "Connect" in t.reason


def test_explain_view_denied_dependency(guild):
    guild.channels[201].overwrites[C3] = Overwrite(C3, "role", allow=P("send_messages"))
    t = explain(guild, guild.members[52], guild.channels[201], ["send_messages"]).traces[0]
    assert not t.allowed and "View" in t.reason


def test_explain_allowed_by_role_overwrite(guild):
    t = explain(guild, guild.members[51], guild.channels[201], ["view_channel"]).traces[0]
    assert t.allowed and "@Level 1" in t.reason


def test_explain_timeout(guild):
    guild.members[50].timed_out = True
    t = explain(guild, guild.members[50], guild.channels[100], ["send_messages"]).traces[0]
    assert not t.allowed and "timed out" in t.reason


def test_explain_render_contains_summary(guild):
    ex = explain(guild, guild.members[50], guild.channels[300])
    out = ex.render(guild)
    assert "Send Messages: NO" in out and "View Channel: YES" in out


def test_unsynced_note(guild):
    guild.channels[201].overwrites[C3] = Overwrite(C3, "role", allow=P("view_channel"))
    ex = explain(guild, guild.members[52], guild.channels[201])
    assert any("NOT synced" in n for n in ex.notes)


def test_guild_level_perm_ignores_channel_overwrites(guild):
    guild.channels[100].overwrites[MOD_ROLE] = Overwrite(MOD_ROLE, "role", deny=P("kick_members"))
    t = explain(guild, guild.members[53], guild.channels[100], ["kick_members"]).traces[0]
    assert t.allowed


def test_flags_normalize_aliases():
    assert F.normalize("send") == "send_messages"
    assert F.normalize("View Channel") == "view_channel"
    assert F.value_of(["admin"]) == 8
    try:
        F.normalize("fly")
        raise AssertionError
    except ValueError:
        pass


def test_known_bit_values():
    # spot-check against Discord documentation
    assert F.FLAGS["view_channel"] == 1 << 10
    assert F.FLAGS["moderate_members"] == 1 << 40
    assert F.FLAGS["manage_roles"] == 268435456
    assert F.FLAGS["send_polls"] == 1 << 49


def test_everyone_constant(guild):
    assert guild.everyone.id == GID
