"""Global identity contract: names are presentation, the Discord user ID is identity — for EVERY event type."""
import json

import pytest

from vrbot.db import Database, LogQuery
from vrbot.events import CATEGORY
from vrbot.identity import IdentityCache, label, snapshot
from vrbot.render import owner_log_line

A, B = 111111111111111111, 222222222222222222
SNAP_A = {"id": A, "username": "actor_a", "display_name": "Actor A", "global_name": None, "bot": False}
SNAP_B = {"id": B, "username": "target_b", "display_name": "Target B", "global_name": None, "bot": False}


@pytest.fixture
async def db():
    d = Database(":memory:")
    await d.open()
    yield d
    await d.close()


@pytest.mark.parametrize("etype", sorted(set(CATEGORY)))
def test_every_event_type_shows_target_and_actor_ids(etype):
    row = {"ts": "2026-10-04T10:00:00+00:00", "type": etype, "category": CATEGORY[etype], "target_id": B,
           "target_name": "Target B", "actor_id": A, "actor_name": "Actor A", "actor_confidence": "confirmed",
           "details": json.dumps({"target_identity": SNAP_B, "actor_identity": SNAP_A, "user_id": B})}
    line = owner_log_line(row)
    assert f"discord.com/users/{B}" in line, (etype, line)
    if etype not in ("member_join", "member_leave"):  # self-events have no separate actor
        assert f"discord.com/users/{A}" in line, (etype, line)


@pytest.mark.parametrize("etype", ["voice_join", "voice_move", "role_add", "timeout_add", "tier_change", "nick_change",
                                   "elevation_grant", "guest_join", "member_leave"])
def test_person_events_without_snapshot_still_show_the_raw_id(etype):
    line = owner_log_line({"ts": "2026-10-04T10:00:00+00:00", "type": etype, "category": "x", "target_id": B,
                           "target_name": "Target B", "details": None})
    assert f"discord.com/users/{B}" in line, (etype, line)


def test_label_always_has_id_and_username():
    assert label(SNAP_B) == "[@Target B](<https://discord.com/users/222222222222222222>)"   # <> = never unfurled
    assert label(None, "OldName", B) == "[@OldName](<https://discord.com/users/222222222222222222>)"
    assert "`" not in label(SNAP_B) and "@target_b" not in label(SNAP_B)   # one clean identity, no ID/username clutter


async def test_add_event_attaches_structured_snapshots(db):
    db.identity_resolver = {A: SNAP_A, B: SNAP_B}.get
    await db.add_event(type="voice_mod_mute", category="voice", guild_id=1, target_id=B, target_name="Target B",
                       actor_id=A, actor_name="Actor A")
    row = (await db.query_events(LogQuery(guild_id=1)))[0]
    det = json.loads(row["details"])
    assert det["target_identity"]["username"] == "target_b" and det["actor_identity"]["id"] == A


async def test_rename_and_departure_resilience(db):
    """Joins as OldName, renamed later, then moderated: one ID links everything; old name still finds it."""
    cache = IdentityCache()
    db.identity_resolver = lambda uid: {**SNAP_B, "display_name": "OldName", "username": "old_user"} if uid == B else None
    await db.add_event(type="member_join", category="membership", guild_id=1, target_id=B, target_name="OldName")
    db.identity_resolver = lambda uid: {**SNAP_B, "display_name": "NewName", "username": "new_user"} if uid == B else SNAP_A
    await db.add_event(type="timeout_add", category="moderation", guild_id=1, target_id=B, target_name="NewName", actor_id=A)
    by_id = await db.query_events(LogQuery(guild_id=1, user_id=B))
    assert len(by_id) == 2
    assert len(await db.query_events(LogQuery(guild_id=1, text="old_user"))) == 1      # old username
    assert len(await db.query_events(LogQuery(guild_id=1, text="NewName"))) == 1       # current name
    cache.remember(SNAP_B)                                                               # last seen identity
    assert cache.get(B)["username"] == "target_b"                                        # still known after leaving


def test_snapshot_from_user_like_object():
    class U:
        id, name, global_name, display_name, bot = B, "target_b", "Tee", "Target B", False
    s = snapshot(U())
    assert s["id"] == B and s["username"] == "target_b" and snapshot(object()) is None


@pytest.mark.parametrize("etype", sorted(set(CATEGORY)))
def test_logs_view_never_crashes_and_shows_ids(etype):
    from vrbot.render import event_line
    row = {"ts": "2026-10-04T10:00:00+00:00", "type": etype, "category": CATEGORY[etype], "target_id": B,
           "target_name": "Target B", "actor_id": A, "actor_name": "Actor A", "actor_confidence": "confirmed",
           "details": json.dumps({"target_identity": SNAP_B, "actor_identity": SNAP_A})}
    line = event_line(row)
    assert f"discord.com/users/{B}" in line and f"discord.com/users/{A}" in line, (etype, line)


def test_trust_and_temporary_access_details_are_rendered():
    base = {"ts": "2026-10-04T10:00:00+00:00", "category": "security", "target_id": B, "actor_id": A,
            "actor_confidence": "confirmed"}
    t = owner_log_line({**base, "type": "tier_change", "details": json.dumps({"from": "tier3", "to": "tier2",
                                                                               "from_name": "Low", "to_name": "Mid"})})
    assert "Low → **Mid**" in t
    g = owner_log_line({**base, "type": "elevation_grant", "details": json.dumps({"kind": "admin", "minutes": 10,
                                                                                   "expires": 1791110000})})
    assert "Temporary Admin" in g and "10 min" in g and "<t:1791110000:t>" in g
