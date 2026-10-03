"""Trust tiers (exactly one) vs temporary authority (always expiring) — pure logic."""
import pytest

from vrbot.trust import (DEFAULT_TIER, Grant, clamp_duration, current_tier, due, grant_key, is_approved_elevation,
                           normalize_plan, plan_set_tier, tiers_held)

T = {"tier1": 1, "tier2": 2, "tier3": 3}
BOT, OWNER, ELEV = 900, 1, {50, 51}


def test_default_tier_is_tier3():
    assert DEFAULT_TIER == "tier3"
    assert normalize_plan([77], T, is_owner_like=False, is_bot=False) == ("tier3", [3], [])


def test_owners_and_bots_get_no_tier():
    assert normalize_plan([], T, is_owner_like=True, is_bot=False) == (None, [], [])
    assert normalize_plan([], T, is_owner_like=False, is_bot=True) == (None, [], [])


def test_promotion_and_demotion_replace():
    assert plan_set_tier([3, 77], "tier2", T) == ([2], [3])     # T3 -> T2
    assert plan_set_tier([2, 77], "tier3", T) == ([3], [2])     # T2 -> T3
    assert plan_set_tier([1], "tier1", T) == ([], [])            # no-op
    with pytest.raises(ValueError):
        plan_set_tier([], "tier9", T)


def test_stacked_roles_normalize_to_highest_trust():
    assert current_tier([3, 2, 1], T) == "tier1"
    assert normalize_plan([2, 3], T, is_owner_like=False, is_bot=False) == ("tier2", [], [3])
    assert tiers_held([1, 2, 3], T) == ["tier1", "tier2", "tier3"]


def test_exactly_one_after_any_plan():
    for start in ([], [1], [2, 3], [1, 2, 3], [3]):
        for target in T:
            add, remove = plan_set_tier(start, target, T)
            end = (set(start) | set(add)) - set(remove)
            assert tiers_held(end, T) == [target]


def test_elevation_durations_never_indefinite():
    assert clamp_duration("mod", None) == 30 and clamp_duration("admin", None) == 10
    assert clamp_duration("admin", 10_000) == 120 and clamp_duration("mod", 10_000) == 1440
    assert clamp_duration("admin", 0) == 10


def test_due_includes_expiries_missed_during_downtime():
    grants = {grant_key(5, "admin"): Grant(5, "admin", 100.0, OWNER).to_dict(),
              grant_key(6, "mod"): Grant(6, "mod", 500.0, OWNER).to_dict()}
    assert [g.user_id for g in due(grants, 200.0)] == [5]       # restart at t=200: overdue admin revoked at once
    assert {g.user_id for g in due(grants, 1000.0)} == {5, 6}


def test_only_bot_issued_recorded_elevation_is_approved():
    active = {grant_key(5, "admin"): Grant(5, "admin", 1e12, OWNER).to_dict()}
    assert is_approved_elevation([50], BOT, BOT, ELEV, active, 5)
    assert not is_approved_elevation([50], OWNER, BOT, ELEV, active, 5)     # added by hand
    assert not is_approved_elevation([50], BOT, BOT, ELEV, active, 6)       # no grant for that user
    assert not is_approved_elevation([50, 99], BOT, BOT, ELEV, active, 5)   # other admin role in same change
    assert not is_approved_elevation([], BOT, BOT, ELEV, active, 5)
