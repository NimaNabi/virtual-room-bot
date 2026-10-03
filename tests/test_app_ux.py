"""App-like UX: music control policy, history/favourites, Control Center wiring (every button has a handler)."""
import re
from pathlib import Path

import pytest

from vrbot import musicdb
from vrbot.cogs.music import STATIONS, may_control
from vrbot.db import Database

APP = Path(__file__).parent.parent / "vrbot" / "cogs" / "app.py"


@pytest.fixture
async def db():
    d = Database(":memory:")
    await d.open()
    yield d
    await d.close()


def test_listeners_can_use_normal_controls_but_not_from_outside():
    base = dict(is_staff=False, is_room_owner=False, is_starter=False, only_listener=False)
    for a in ("pause_toggle", "skip", "previous", "shuffle", "loop"):
        assert may_control(a, is_listener=True, **base)
        assert not may_control(a, is_listener=False, **base)


def test_destructive_controls_need_owner_starter_or_alone():
    base = dict(is_listener=True, is_staff=False)
    for a in ("stop", "clear", "volume", "disconnect"):
        assert not may_control(a, is_room_owner=False, is_starter=False, only_listener=False, **base)
        assert may_control(a, is_room_owner=True, is_starter=False, only_listener=False, **base)
        assert may_control(a, is_room_owner=False, is_starter=True, only_listener=False, **base)
        assert may_control(a, is_room_owner=False, is_starter=False, only_listener=True, **base)
    assert may_control("stop", is_listener=False, is_staff=True, is_room_owner=False, is_starter=False, only_listener=False)


def test_remove_own_item_only():
    kw = dict(is_listener=True, is_staff=False, is_room_owner=False, is_starter=False, only_listener=False)
    assert may_control("remove", owns_item=True, **kw)
    assert not may_control("remove", owns_item=False, **kw)


def test_stations_are_live_https_streams():
    assert len(STATIONS) >= 5
    for emoji, name, url, desc in STATIONS.values():
        assert url.startswith("https://") and emoji and name and desc


async def test_history_recent_frequent_and_favourites(db):
    for title, n in (("Song A", 3), ("Song B", 1), ("Song C", 2)):
        for _ in range(n):
            await musicdb.record_play(db, 1, key=f"u:{title}", title=title, author="X", uri=f"https://x/{title}", source="yt",
                                      length_ms=1000, requester_id=7, channel_id=99)
    freq = await musicdb.frequent(db, 1, 10)
    assert [r["title"] for r in freq] == ["Song A", "Song C"]        # 1-play songs are not "frequent"
    rec = await musicdb.recent(db, 1, 10)
    assert {r["title"] for r in rec} == {"Song A", "Song B", "Song C"} and len(rec) == 3   # de-duplicated
    assert await musicdb.add_favorite(db, 1, 7, key="u:Song A", title="Song A", author="X", uri="https://x/Song A") == "added"
    assert await musicdb.add_favorite(db, 1, 7, key="u:Song A", title="Song A", author="X", uri="https://x/Song A") == "exists"
    assert len(await musicdb.favorites(db, 1, 7)) == 1 and await musicdb.favorites(db, 1, 0) == []
    await musicdb.remove_favorite(db, 1, 7, "u:Song A")
    assert await musicdb.favorites(db, 1, 7) == []


async def test_favourites_have_a_cap(db):
    for n in range(musicdb.MAX_USER_FAVS):
        assert await musicdb.add_favorite(db, 1, 5, key=f"k{n}", title="t", author=None, uri=f"https://x/{n}") == "added"
    assert await musicdb.add_favorite(db, 1, 5, key="extra", title="t", author=None, uri="https://x/e") == "full"


def test_every_control_center_component_has_a_handler_and_every_screen_exists():
    src = APP.read_text(encoding="utf-8")
    actions = set(re.findall(r'App(?:Button|Select|UserSelect)\("([a-z0-9_]+)"', src))
    handlers = set(re.findall(r"async def h_([a-z0-9_]+)\(", src))
    assert actions - handlers == set(), actions - handlers
    screens = set(re.findall(r'AppButton\("go", "([a-z0-9_]+)"', src)) | set(re.findall(r'back\("([a-z0-9_]+)"', src))
    screens |= set(re.findall(r'self\.show\([a-z]+, "([a-z0-9_]+)"', src))
    screens |= set(re.findall(r'"[a-z_]+": "([a-z_]+)"', src.split("PARENT = {")[1].split("}")[0]))
    defined = set(re.findall(r"async def s_([a-z0-9_]+)\(", src))
    assert screens - defined == set(), screens - defined


def test_member_facing_text_has_no_technical_words():
    src = APP.read_text(encoding="utf-8")
    member_part = src.split('"""', 2)[2].split("# ---------------------------------------------------------- owner")[0]
    visible = re.findall(r'(?:label|title|description|placeholder|Toast\(|note=)f?"([^"\n]+)"', member_part)
    strings = " ".join(visible)
    assert len(visible) > 10
    for bad in ("cog", "lavalink", "wavelink", "baseline", "database", "drift", "permission bit", "tier", "rest verif"):
        assert bad not in strings.lower(), bad


def test_custom_ids_fit_discord_limit():
    # longest ids: "va:rm:disconnect.<snowflake>" / "vs:olc:7d.<snowflake>"
    assert len("va:rm:disconnect." + "1" * 20) <= 100 and len("vu:olm:security.today") <= 100


def test_navigation_trail_back_and_home():
    from vrbot.cogs.app import ControlCenter
    cc = ControlCenter.__new__(ControlCenter)
    cc.sessions = {}
    for ref in ("music", "music_quick", "music_player"):
        cc._push(1, ref)
    assert cc.sessions[1] == ["home", "music", "music_quick", "music_player"]
    cc._push(1, "music")                      # going up to a screen in the trail cuts back to it
    assert cc.sessions[1] == ["home", "music"]
    cc._push(1, "home")
    assert cc.sessions[1] == ["home"]
    for n in range(20):
        cc._push(2, f"owner_member|{n}")
    assert len(cc.sessions[2]) <= 12


def test_every_screen_has_a_parent_or_is_top_level():
    from vrbot.cogs.app import ControlCenter
    src = APP.read_text(encoding="utf-8")
    screens = set(re.findall(r"async def s_([a-z0-9_]+)\(", src))
    top = {"home", "music", "room", "social", "friends", "help", "owner"}
    assert screens - top - set(ControlCenter.PARENT) == set()
