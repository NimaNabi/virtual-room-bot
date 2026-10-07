"""Setup wizard + configurable identity: generic defaults, custom names/colours, mapping, no hardcoded IDs."""
import re
from pathlib import Path

import pytest

from vrbot.cogs.setup import (CATEGORIES, CHANNELS, DEFAULT_NAMES, KEY, READ_ONLY, ROOM_ACCESS, channel_name, parse_color,
                              plan_items)
from vrbot.config import ServerConfig
from vrbot.trust import tier_role_ids

ROOT = Path(__file__).parent.parent
PLACEHOLDERS = {"123456789012345678", "234567890123456789", "345678901234567890", "456789012345678901",
                "567890123456789012", "111111111111111111", "222222222222222222", "333333333333333333"}


def test_minimal_existing_and_recommended_plans():
    cats_min, ch_min = plan_items("minimal")
    assert plan_items("existing") == (cats_min, ch_min)
    cats_rec, ch_rec = plan_items("recommended")
    assert {"hub", "alerts", "voice_log", "server_log"} <= set(ch_min) & set(ch_rec)
    assert set(cats_min) <= set(cats_rec)
    for plan in ("minimal", "recommended"):
        cats, chans = plan_items(plan)
        assert all(parent in cats for _, parent, _ in chans.values())


def test_private_rooms_are_cumulative():
    assert set(ROOM_ACCESS["room_tier1"]) <= set(ROOM_ACCESS["room_tier2"]) <= set(ROOM_ACCESS["room_tier3"])
    assert READ_ONLY <= set(CHANNELS) and set(CATEGORIES)


@pytest.mark.parametrize("names", [{"trust_level_1": "Core", "trust_level_2": "Social", "trust_level_3": "Member"},
                                   {"trust_level_1": "Friends", "trust_level_2": "Members", "trust_level_3": "Guests"}])
def test_custom_level_names_flow_into_room_names(names):
    for t, key in KEY.items():
        assert channel_name(f"room_{t}", {}, names) == f"{names[key]} room"
    assert channel_name("room_tier1", {"room_tier1": "🔒 {level}"}, names) == f"🔒 {names['trust_level_1']}"


def test_layout_names_are_overridable_and_generic_by_default():
    assert channel_name("chat", {}, {}) == "chat" and channel_name("chat", {"chat": "general"}, {}) == "general"
    assert channel_name("menu_min", {}, {}) == DEFAULT_NAMES["menu"]


def test_colours_are_parsed_and_validated():
    assert parse_color("#4A90D9") == 0x4A90D9 and parse_color("50b37a") == 0x50B37A and parse_color("") == 0
    with pytest.raises(ValueError):
        parse_color("blue")


def test_display_names_never_change_security_identity():
    a = ServerConfig.model_validate({"trust": {"names": {"trust_level_1": "Potato"}},
                                     "baseline": {"trust_levels": {"trust_level_1": {"role": "11", "rank": 1},
                                                                   "trust_level_2": {"role": "12", "rank": 2},
                                                                   "trust_level_3": {"role": "13", "rank": 3}}}})
    b = ServerConfig.model_validate({"trust": {"names": {"trust_level_1": "Core"}},
                                     "baseline": a.baseline.model_dump()})
    assert tier_role_ids(a) == tier_role_ids(b) == {"tier1": 11, "tier2": 12, "tier3": 13}


def test_existing_roles_are_used_as_the_levels():
    # a server that already has Friends / Members / Guests maps them; no product role names are needed
    c = ServerConfig.model_validate({"baseline": {"trust_levels": {"trust_level_1": {"role": "21", "rank": 1},
                                                                   "trust_level_2": {"role": "22", "rank": 2},
                                                                   "trust_level_3": {"role": "23", "rank": 3}}},
                                     "trust": {"names": {"trust_level_1": "Friends", "trust_level_2": "Members",
                                                         "trust_level_3": "Guests"}}})
    assert tier_role_ids(c) == {"tier1": 21, "tier2": 22, "tier3": 23}


def test_fresh_owner_and_owner_equivalents_are_configuration_only():
    c = ServerConfig()
    assert c.privacy.owner_id is None and c.privacy.approved_owner_equivalents == []
    c.privacy.owner_id = 7
    c.privacy.approved_owner_equivalents = [8]
    assert c.privacy.is_owner_like(7) and c.privacy.is_owner_like(8) and c.privacy.is_owner_like(9, guild_owner_id=9)
    assert not c.privacy.is_owner_like(10, guild_owner_id=9)


def test_no_discord_ids_in_shipped_text_files():
    pat = re.compile(r"(?<!\d)\d{17,20}(?!\d)")
    for p in ROOT.rglob("*"):
        local = {".git", "runtime", "data", "logs", "previous", ".venv"}   # installed/user data, not shipped files
        if p.is_file() and p.suffix in {".py", ".yaml", ".yml", ".md", ".example", ".txt", ".ini"} and not local & set(p.relative_to(ROOT).parts):
            found = set(pat.findall(p.read_text(encoding="utf-8", errors="ignore"))) - PLACEHOLDERS
            assert not found, p
