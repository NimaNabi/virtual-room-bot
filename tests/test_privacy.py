"""Privacy floors: Owner area → Level 1 → Level 2 → Level 3 → public (the owner's source-of-truth matrix)."""
import pytest

from vrbot.config import ServerConfig
from vrbot.perms import flags as F
from vrbot.perms.audit import audit, plan_repair
from vrbot.perms.engine import compute
from vrbot.perms.model import Channel, Guild, Member, Overwrite, Role
from vrbot.perms.privacy import member_tier, privacy_audit, visibility_matrix

GID, OWNER, BOT = 500, 1, 2
C1, C2, C3, BOTR, ADMINR = 11, 12, 13, 14, 15
L0, R1, R2, R3, PUB, CAT, LOGS = 100, 101, 102, 103, 104, 200, 105
EV = F.value_of(["view_channel", "send_messages", "read_message_history", "connect", "speak", "use_application_commands"])
FULL = F.value_of(["view_channel", "connect", "speak", "stream", "send_messages", "read_message_history"])
HIDE = F.value_of(["view_channel", "connect"])


def floors() -> Guild:
    g = Guild(GID, "VR", OWNER)
    g.roles = {GID: Role(GID, "@everyone", EV, 0), C3: Role(C3, "RoleC", 0, 1), C2: Role(C2, "RoleB", 0, 2),
               C1: Role(C1, "RoleA", 0, 3), ADMINR: Role(ADMINR, "Admins", 0, 4),
               BOTR: Role(BOTR, "Bot", F.FLAGS["administrator"], 5, managed=True)}
    ev_hide = lambda: Overwrite(GID, "role", deny=HIDE)  # noqa: E731
    allow = lambda r: Overwrite(r, "role", allow=FULL)  # noqa: E731
    g.channels = {
        CAT: Channel(CAT, "CIRCLES", "category", overwrites={GID: ev_hide(), C1: Overwrite(C1, "role", allow=F.FLAGS["view_channel"]),
                                                             C2: Overwrite(C2, "role", allow=F.FLAGS["view_channel"]),
                                                             C3: Overwrite(C3, "role", allow=F.FLAGS["view_channel"])}),
        L0: Channel(L0, "0", "voice", parent_id=CAT, overwrites={GID: ev_hide()}),
        R1: Channel(R1, "Level 1", "voice", parent_id=CAT, overwrites={GID: ev_hide(), C1: allow(C1)}),
        R2: Channel(R2, "Level 2", "voice", parent_id=CAT, overwrites={GID: ev_hide(), C1: allow(C1), C2: allow(C2)}),
        R3: Channel(R3, "Level 3", "voice", parent_id=CAT, overwrites={GID: ev_hide(), C1: allow(C1), C2: allow(C2), C3: allow(C3)}),
        PUB: Channel(PUB, "Lobby", "voice"),
        LOGS: Channel(LOGS, "voice-activity", "text", overwrites={GID: Overwrite(GID, "role", deny=F.FLAGS["view_channel"])}),
    }
    g.members = {OWNER: Member(OWNER, "TestOwner", [ADMINR]), BOT: Member(BOT, "VRB", [BOTR], bot=True),
                 21: Member(21, "one", [C1]), 22: Member(22, "two", [C2]), 23: Member(23, "three", [C3]),
                 24: Member(24, "nobody", []), 25: Member(25, "one+three", [C1, C3]), 26: Member(26, "two+three", [C2, C3])}
    return g


def cfg() -> ServerConfig:
    return ServerConfig.model_validate({
        "baseline": {"trust_levels": {"trust_level_1": {"role": str(C1), "rank": 1, "allow_dangerous": []},
                                 "trust_level_2": {"role": str(C2), "rank": 2}, "trust_level_3": {"role": str(C3), "rank": 3}}},
        "privacy": {"owner_id": OWNER, "tiers": {
            "owner_area": {"rank": 0}, "trust_level_1": {"rank": 1, "roles": [str(C1)]}, "trust_level_2": {"rank": 2, "roles": [str(C2)]},
            "trust_level_3": {"rank": 3, "roles": [str(C3)]}, "public": {"rank": 4}},
            "areas": {str(CAT): "trust_level_3", str(L0): "owner_area", str(R1): "trust_level_1", str(R2): "trust_level_2", str(R3): "trust_level_3",
                      str(PUB): "public", str(LOGS): "owner_area"}},
    })


def sees(g, mid, cid):
    return bool(compute(g, g.members[mid], g.channels[cid]).value & F.FLAGS["view_channel"])


EXPECTED = {  # member -> (L0, R1, R2, R3, PUB)
    OWNER: (1, 1, 1, 1, 1), 21: (0, 1, 1, 1, 1), 22: (0, 0, 1, 1, 1), 23: (0, 0, 0, 1, 1), 24: (0, 0, 0, 0, 1),
    25: (0, 1, 1, 1, 1),  # Level 1 + Level 3 = Level 1
    26: (0, 0, 1, 1, 1),  # Level 2 + Level 3 = Level 2
}


@pytest.mark.parametrize("mid", list(EXPECTED))
def test_floor_matrix(mid):
    g = floors()
    got = tuple(int(sees(g, mid, c)) for c in (L0, R1, R2, R3, PUB))
    assert got == EXPECTED[mid]


@pytest.mark.parametrize("mid", [21, 22, 23, 24, 25, 26])
def test_hidden_rooms_hide_voice_chat_text_too(mid):
    g = floors()
    for cid in (L0, R1, R2, R3):
        v = compute(g, g.members[mid], g.channels[cid]).value
        if not v & F.FLAGS["view_channel"]:
            assert not v & (F.FLAGS["read_message_history"] | F.FLAGS["send_messages"] | F.FLAGS["connect"])


def test_nobody_but_owner_sees_owner_logs():
    g = floors()
    assert sees(g, OWNER, LOGS)
    assert not any(sees(g, m, LOGS) for m in (21, 22, 23, 24, 25, 26))


def test_member_tier_most_private_wins():
    p = cfg().privacy
    assert member_tier(p, {C1, C3}) == "trust_level_1"
    assert member_tier(p, {C3}) == "trust_level_3"
    assert member_tier(p, set()) == "public"


def test_privacy_audit_clean_on_correct_floors():
    g = floors()
    assert [x for x in privacy_audit(g, cfg(), BOT) if x.severity != "INFO"] == []


def test_detects_lower_tier_seeing_higher_floor():
    g = floors()
    g.channels[R1].overwrites[C3] = Overwrite(C3, "role", allow=F.FLAGS["view_channel"])
    leaks = privacy_audit(g, cfg(), BOT)
    assert any(x.code == "privacy_leak" and "three" in x.title and "Level 1" in x.title for x in leaks)
    assert any(x.code == "privacy_tier_leak" for x in leaks)


def test_admin_bypass_is_critical():
    g = floors()
    g.roles[ADMINR].permissions = F.FLAGS["administrator"]
    g.members[27] = Member(27, "TestUserA", [ADMINR])
    leaks = privacy_audit(g, cfg(), BOT)
    assert any(x.code == "privacy_admin_bypass" and "TestUserA" in x.title for x in leaks)
    assert any(x.code == "privacy_leak" and "TestUserA" in x.title and "#0" in x.title for x in leaks)
    assert not any("TestOwner " in x.title or x.title.startswith("TestOwner ") for x in leaks)  # canonical owner is exempt


def test_voice_powers_flagged():
    g = floors()
    g.roles[C1].permissions |= F.FLAGS["move_members"]
    assert any(x.code == "privacy_voice_powers" for x in privacy_audit(g, cfg(), BOT))


def test_temporary_pass_is_allowed_standing_pass_is_not():
    g = floors()
    g.channels[L0].overwrites[22] = Overwrite(22, "member", allow=HIDE)
    assert any(x.code == "privacy_leak" for x in privacy_audit(g, cfg(), BOT))
    assert not [x for x in privacy_audit(g, cfg(), BOT, {(L0, 22)}) if x.severity != "INFO"]


def test_visibility_matrix():
    m = visibility_matrix(floors(), cfg())
    assert set(m["trust_level_1"]) == {"trust_level_1", "trust_level_2", "trust_level_3", "public"}
    assert set(m["trust_level_3"]) == {"trust_level_3", "public"}
    assert set(m["public"]) == {"public"}


def test_tier_expectations_feed_repair():
    """A mistaken grant (Level 3 can see Level 1's room) is found by the normal auditor and repaired to View=NO."""
    g = floors()
    g.channels[R1].overwrites[C3] = Overwrite(C3, "role", allow=F.FLAGS["view_channel"])
    c = cfg()
    fs = [f for f in audit(g, c, BOT) if f.code == "channel_access" and f.trust_level == "trust_level_3"]
    assert fs and fs[0].severity == "CRITICAL"
    plan = plan_repair(g, c, BOT, scope="trust_level_3")
    assert plan.changes and not plan.introduced
    sim = g.clone()
    for ch in plan.changes:
        if ch.kind == "overwrite":
            if ch.after is None:
                sim.channels[ch.channel_id].overwrites.pop(ch.target_id, None)
            else:
                sim.channels[ch.channel_id].overwrites[ch.target_id] = Overwrite(ch.target_id, ch.target_type, *ch.after)
    assert not sees(sim, 23, R1)


# ---------------------------------------------------------------- social privacy
from vrbot.perms.privacy import filter_events_for_viewer, social_findings, visible_channel_ids  # noqa: E402


def test_neutral_names_pass_social_check():
    assert social_findings(floors(), cfg().privacy) == []


@pytest.mark.parametrize("bad", ["Level 1", "Tier 2", "VIP", "⭐ Friends", "Gold", "Inner trust_level", "Level 3", "Core"])
def test_rank_revealing_names_flagged(bad):
    g = floors()
    g.roles[C2].name = bad
    assert any(x.code == "social_rank_name" for x in social_findings(g, cfg().privacy))


def test_hoist_mentionable_and_colour_progression_flagged():
    g = floors()
    g.roles[C1].hoist = True
    g.roles[C2].mentionable = True
    g.roles[C3].color = 0xF1C40F
    codes = {x.code for x in social_findings(g, cfg().privacy)}
    assert {"social_hoist", "social_mentionable", "social_colors"} <= codes


def test_admin_bypass_wording_and_approved_owner_equivalent():
    g = floors()
    g.roles[ADMINR].permissions = F.FLAGS["administrator"]
    g.members[27] = Member(27, "TestUserA", [ADMINR])
    leaks = privacy_audit(g, cfg(), BOT)
    assert any(x.title == "Unauthorized Administrator bypass: TestUserA" and x.severity == "CRITICAL" for x in leaks)
    c = cfg()
    c.privacy.approved_owner_equivalents = [27]
    leaks = privacy_audit(g, c, BOT)
    assert any(x.title == "Approved owner-equivalent: TestUserA" and x.severity == "INFO" for x in leaks)
    assert not any("TestUserA" in x.title and x.severity == "CRITICAL" for x in leaks)


def test_other_admins_are_never_exempted():
    g = floors()
    g.roles[ADMINR].permissions = F.FLAGS["administrator"]
    g.members[27] = Member(27, "TestUserA", [ADMINR])
    g.members[28] = Member(28, "OtherAdmin", [ADMINR])
    c = cfg()
    c.privacy.approved_owner_equivalents = [27]
    assert any(x.title == "Unauthorized Administrator bypass: OtherAdmin" for x in privacy_audit(g, c, BOT))


def test_visible_channels_per_floor():
    g = floors()
    assert L0 not in visible_channel_ids(g, 21) and R1 in visible_channel_ids(g, 21)
    assert visible_channel_ids(g, 23) & {L0, R1, R2, LOGS} == set()
    assert visible_channel_ids(g, 999) == set()


def test_non_owner_event_filter_is_data_layer():
    g = floors()
    rows = [
        {"category": "voice", "target_id": 22, "channel_id": R2, "type": "voice_join"},         # own, visible: yes
        {"category": "voice", "target_id": 22, "channel_id": L0, "type": "voice_join"},         # own but Owner area: no
        {"category": "voice", "target_id": 1, "channel_id": R2, "type": "voice_join"},          # someone else: no
        {"category": "security", "target_id": 22, "channel_id": None, "type": "guardian_alert"},  # guardian: no
        {"category": "structure", "target_id": 22, "channel_id": R2, "type": "channel_update"},  # admin history: no
        {"category": "membership", "target_id": 22, "channel_id": None, "type": "member_join"},  # own: yes
    ]
    out = filter_events_for_viewer(rows, visible_channel_ids(g, 22), 22)
    assert [(e["type"], e["channel_id"]) for e in out] == [("voice_join", R2), ("member_join", None)]


def test_non_owner_ai_is_not_offered_owner_tools():
    from vrbot.ai.tools import SCHEMAS, schemas_for
    names = {s["function"]["name"] for s in schemas_for(False)}
    assert not names & {"permission_problems", "changes_since", "propose_repair"}
    assert len(schemas_for(True)) == len(SCHEMAS)


def test_room_name_filter():
    from vrbot.cogs.voicerooms import clean_name
    assert clean_name("join discord.gg/abc now") == "join abc now"
    assert "@everyone" not in clean_name("@everyone party")
    assert clean_name("https://x.y") == "x.y" or "http" not in clean_name("https://x.y")
    assert clean_name("") == "Room" and len(clean_name("x" * 200)) == 60


def test_invite_diff():
    from vrbot.cogs.invites import diff_invites
    assert diff_invites({"a": 1, "b": 5}, {"a": 2, "b": 5}) == ["a"]
    assert diff_invites({"a": 1}, {"a": 1, "new": 1}) == ["new"]
    assert diff_invites({"a": 1}, {"a": 1}) == []


def test_default_config_social_privacy():
    from pathlib import Path

    from vrbot.config import load_server_config
    c = load_server_config(Path(__file__).parent.parent / "config" / "default.yaml")
    assert not c.privacy.social_privacy and c.privacy.approved_owner_equivalents == []
    assert c.baseline.enforce_trust_level_order is False


def test_staff_redaction_hides_owner_area_and_security():
    from vrbot.perms.privacy import redact_for_staff
    g = floors()
    rows = [{"category": "voice", "channel_id": L0}, {"category": "voice", "channel_id": R3},
            {"category": "security", "channel_id": None}, {"category": "moderation", "channel_id": None}]
    out = redact_for_staff(rows, visible_channel_ids(g, 23))
    assert out == [{"category": "voice", "channel_id": R3}, {"category": "moderation", "channel_id": None}]


def test_temporary_admin_is_info_not_critical_and_elevation_role_not_flagged():
    g = floors()
    g.roles[ADMINR].permissions = F.FLAGS["administrator"]
    g.members[29] = Member(29, "Helper", [ADMINR])
    g.settings["temporary_admins"] = [29]
    leaks = privacy_audit(g, cfg(), BOT)
    assert any(x.severity == "INFO" and x.title.startswith("Temporary Administrator") for x in leaks)
    assert not any("Helper" in x.title and x.severity == "CRITICAL" for x in leaks)
    g.settings["elevation_roles"] = [ADMINR]
    assert not any(f.code == "unexpected_admin_role" for f in audit(g, cfg(), BOT))
