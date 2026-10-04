"""Member join/leave/kick/ban carry a permanent identity (user ID) and stay findable after the person left."""
import json
from datetime import datetime, timezone

import pytest

from vrbot.cogs.eventlog import EventLog
from vrbot.config import ServerConfig
from vrbot.db import Database, LogQuery
from vrbot.render import owner_log_line

UID = 234567890123456789


@pytest.fixture
async def db():
    d = Database(":memory:")
    await d.open()
    yield d
    await d.close()


def _ev(t, det, **kw):
    return {"ts": "2026-10-01T10:00:00+00:00", "type": t, "target_id": UID, "target_name": "TestUserA",
            "details": json.dumps(det), "category": "membership", **kw}


def test_leave_line_has_permanent_identity_and_honest_cause():
    det = {"user_id": UID, "username": "testusera", "display_name": "TestUserA", "trust_level": "Member",
           "joined_at": "2025-01-02T00:00:00+00:00", "account_created": "2020-05-06T00:00:00+00:00",
           "sponsor_name": "TestUserB", "via": "guest invite", "cause": "no kick or ban recorded"}
    line = owner_log_line(_ev("member_leave", det))
    for part in ("Member left", f"[@TestUserA](https://discord.com/users/{UID})", "level **Member**", "joined <t:",
                 "account created <t:", "joined via @TestUserB (guest invite)", "no kick or ban recorded"):
        assert part in line, part


def test_old_events_without_details_still_show_the_id():
    line = owner_log_line({**_ev("member_leave", {}), "details": None})
    assert f"https://discord.com/users/{UID}" in line and "@TestUserA" in line


def test_kick_shows_actor_or_unknown():
    assert "by @Mod" in owner_log_line(_ev("member_kick", {"user_id": UID}, actor_name="Mod"))
    assert "actor unknown" in owner_log_line(_ev("member_ban", {"user_id": UID}))


class _User:  # a user who is no longer a cached member (worst case)
    id, name, global_name, display_name, bot = UID, "testusera", "Test A", "TestUserA", False
    created_at = datetime(2020, 5, 6, tzinfo=timezone.utc)


class _Guild:
    id = 1

    def get_role(self, rid):
        return None


class _Bot:
    def __init__(self, db):
        self.db, self.cfg = db, ServerConfig()


async def test_identity_falls_back_to_recorded_history(db):
    await db.add_event(type="member_join", category="membership", guild_id=1, target_id=UID, target_name="TestUserA")
    await db.add_event(type="invite_used", category="membership", guild_id=1, target_id=UID, actor_id=5,
                       actor_name="TestUserB", actor_confidence="confirmed")
    ident = await EventLog(_Bot(db)).member_identity(_Guild(), _User())
    assert ident["user_id"] == UID and ident["username"] == "testusera" and ident["global_name"] == "Test A"
    assert ident["joined_at"] and ident["sponsor_name"] == "TestUserB" and ident["account_age_days"] > 365
    assert ident["member_cached"] is False


async def test_departed_member_is_searchable_by_username_and_id(db):
    await db.add_event(type="member_leave", category="membership", guild_id=1, target_id=UID, target_name="TestUserA",
                       details={"user_id": UID, "username": "uniqueuser42"})
    assert len(await db.query_events(LogQuery(guild_id=1, text="uniqueuser42"))) == 1
    assert len(await db.query_events(LogQuery(guild_id=1, target_id=UID))) == 1
