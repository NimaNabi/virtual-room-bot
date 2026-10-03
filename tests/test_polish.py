"""Full-polish pass: cumulative tier profiles, monotonic capabilities, colour neutrality, guests, fun, presence."""
import random
import re

from vrbot.cogs.fun import split_teams, toggle
from vrbot.cogs.guests import can_sponsor, standard_access_plan, within_limits
from vrbot.cogs.presence import settle
from vrbot.guide import SECTIONS
from vrbot.perms import flags as F
from vrbot.perms.model import Channel, Guild, Member, Overwrite, Role
from vrbot.trust import (NEVER_FOR_TIERS, TIER1_PERMS, TIER2_PERMS, TIER3_PERMS, capability_matrix, is_prestige_color,
                           rel_luminance)

T = {"tier1": 1, "tier2": 2, "tier3": 3}
PALETTE = [0x8E7CC3, 0x4A9C8C, 0xC7676B]  # synthetic equal-weight palette


def test_profiles_are_cumulative_and_never_destructive():
    assert set(TIER3_PERMS) <= set(TIER2_PERMS) <= set(TIER1_PERMS)
    assert not set(TIER1_PERMS) & set(NEVER_FOR_TIERS)
    for name in TIER1_PERMS + NEVER_FOR_TIERS:
        assert name in F.FLAGS, name
    assert "set_voice_channel_status" in F.FLAGS and F.FLAGS["set_voice_channel_status"] == 1 << 48


def test_permission_preset_is_cumulative_configurable_and_safe():
    from vrbot.config import ServerConfig
    from vrbot.trust import NEVER_FOR_TIERS, tier_permissions
    base = tier_permissions(ServerConfig())
    assert "send_messages" in base["tier3"] and "create_instant_invite" in base["tier1"]
    assert "create_instant_invite" not in base["tier3"]
    c = ServerConfig.model_validate({"trust": {"permissions": {"trust_level_2": ["priority_speaker", "administrator", "ban_members"]}}})
    p = tier_permissions(c)
    assert "priority_speaker" in p["tier2"] and "priority_speaker" in p["tier1"] and "priority_speaker" not in p["tier3"]
    for tier in p.values():
        assert not set(tier) & set(NEVER_FOR_TIERS)


def _guild(tier_role_perms, overwrites=None):
    g = Guild(id=100, name="g", owner_id=1)
    g.roles[100] = Role(100, "@everyone", F.value_of(["view_channel", "connect"]), 0)
    for t, rid in T.items():
        g.roles[rid] = Role(rid, t, F.value_of(tier_role_perms[t]), rid)
    g.channels[50] = Channel(50, "room", "voice", overwrites=overwrites or {})
    return g


def test_capability_matrix_monotonic_with_profiles():
    g = _guild({"tier1": TIER1_PERMS, "tier2": TIER2_PERMS, "tier3": TIER3_PERMS})
    assert capability_matrix(g, T)["violations"] == []


def test_capability_matrix_detects_inversion():
    ow = {2: Overwrite(2, "role", deny=F.FLAGS["stream"])}  # Tier 2 denied stream while Tier 3 has it
    g = _guild({"tier1": TIER1_PERMS, "tier2": TIER2_PERMS, "tier3": TIER3_PERMS}, ow)
    v = capability_matrix(g, T)["violations"]
    assert any(x["cap"] == "stream" and x["higher"] == "tier2" and x["lower"] == "tier3" for x in v)


def test_palette_has_no_medal_colours_and_equal_weight():
    assert not any(is_prestige_color(c) for c in PALETTE)
    lum = [rel_luminance(c) for c in PALETTE]
    assert max(lum) - min(lum) < 0.08
    assert is_prestige_color(0xD4AF37) and is_prestige_color(0xC0C0C0) and is_prestige_color(0x8C5A2B)


def test_default_level_names_are_generic_and_configurable():
    from vrbot.config import ServerConfig
    c = ServerConfig()
    assert c.trust.names == {"trust_level_1": "Trusted", "trust_level_2": "Standard", "trust_level_3": "Member"}
    c2 = ServerConfig.model_validate({"trust": {"names": {"trust_level_1": "Potato"}}})
    assert c2.trust.names["trust_level_1"] == "Potato"


def test_guest_sponsor_is_tier1_or_owner_only():
    assert can_sponsor([1], T, False) and can_sponsor([], T, True)
    assert not can_sponsor([2], T, False) and not can_sponsor([3], T, False)


def test_standard_access_only_default_tier_for_untiered():
    assert standard_access_plan([], T, is_bot=False, owner_like=False) == (True, "tier3")
    for held in ([1], [2], [3]):      # never demote, never touch someone who has a level
        assert standard_access_plan(held, T, is_bot=False, owner_like=False)[0] is False
    assert standard_access_plan([], T, is_bot=True, owner_like=False)[0] is False
    assert standard_access_plan([], T, is_bot=False, owner_like=True)[0] is False


def test_guest_rate_limit():
    now = 100_000.0
    assert within_limits([now - 90_000] * 20, now, 8)          # old ones don't count
    assert not within_limits([now - 10] * 8, now, 8)


def test_tonight_one_choice_per_person():
    s = toggle({"picks": {}}, "gaming", 7)
    s = toggle(s, "movie", 7)
    assert s["picks"]["gaming"] == [] and s["picks"]["movie"] == [7]
    s = toggle(s, "movie", 7)
    assert s["picks"]["movie"] == []


def test_split_teams_keeps_everyone_once():
    people = list("abcdefg")
    teams = split_teams(people, 3, random.Random(1))
    assert sorted(sum(teams, [])) == people and len(teams) == 3
    assert max(map(len, teams)) - min(map(len, teams)) <= 1


def test_presence_debounce_ignores_flaps():
    pending, last = {5: ("online", 0.0)}, {5: "offline"}
    assert settle(pending, last, 60.0) == []                  # not settled yet
    assert settle(pending, last, 130.0) == [(5, "online")]
    pending[5] = ("online", 200.0)                            # flapped back to the same status
    assert settle(pending, last, 400.0) == []


def test_member_guide_never_mentions_tiers_or_owner_tools():
    bad = re.compile(r"tier|owner area|/tier|/access|/guardian|/trust_level|owner tools", re.I)
    for _, title, _, text in SECTIONS.values():
        assert not bad.search(title + text)
