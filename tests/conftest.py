import pytest

from vrbot.config import ServerConfig
from vrbot.perms import flags as F
from vrbot.perms.model import Channel, Guild, Member, Overwrite, Role

GID = 1000
OWNER = 1
BOT = 2
ADMIN_ROLE, MOD_ROLE, BOTROLE, C1, C2, C3 = 10, 11, 12, 21, 22, 23
DEFAULT_EVERYONE = F.value_of(["view_channel", "send_messages", "read_message_history", "connect", "speak",
                               "add_reactions", "embed_links", "attach_files", "use_vad", "change_nickname"])


def P(*names):
    return F.value_of(names)


@pytest.fixture
def guild() -> Guild:
    g = Guild(id=GID, name="TestOwner", owner_id=OWNER)
    g.roles = {
        GID: Role(GID, "@everyone", DEFAULT_EVERYONE, 0),
        C3: Role(C3, "Level 3", 0, 1),
        C2: Role(C2, "Level 2", 0, 2),
        C1: Role(C1, "Level 1", 0, 3),
        MOD_ROLE: Role(MOD_ROLE, "Moderator", P("kick_members", "ban_members", "moderate_members", "manage_messages"), 4),
        BOTROLE: Role(BOTROLE, "ServerBot", P("view_channel", "send_messages", "embed_links", "attach_files",
                                             "read_message_history", "view_audit_log", "manage_roles", "manage_channels",
                                             "kick_members", "ban_members", "moderate_members", "manage_messages",
                                             "connect", "speak"), 5, managed=True),
        ADMIN_ROLE: Role(ADMIN_ROLE, "Admin", P("administrator"), 6),
    }
    g.channels = {
        100: Channel(100, "general", "text", position=0),
        101: Channel(101, "announcements", "text", position=1, overwrites={
            GID: Overwrite(GID, "role", deny=P("send_messages"))}),
        200: Channel(200, "Level 1", "category", position=2, overwrites={
            GID: Overwrite(GID, "role", deny=P("view_channel")),
            C1: Overwrite(C1, "role", allow=P("view_channel"))}),
        201: Channel(201, "inner-trust_level", "text", parent_id=200, position=0, overwrites={
            GID: Overwrite(GID, "role", deny=P("view_channel")),
            C1: Overwrite(C1, "role", allow=P("view_channel"))}),
        202: Channel(202, "inner-voice", "voice", parent_id=200, position=1, overwrites={
            GID: Overwrite(GID, "role", deny=P("view_channel")),
            C1: Overwrite(C1, "role", allow=P("view_channel")),
            C2: Overwrite(C2, "role", allow=P("view_channel"))}),   # MISTAKE: trust_level_2 can see trust_level_1 voice
        300: Channel(300, "gaming", "text", position=3, overwrites={
            C2: Overwrite(C2, "role", deny=P("send_messages"))}),   # MISTAKE: trust_level_2 can't talk in gaming
        301: Channel(301, "Gaming VC", "voice", position=4, overwrites={
            C3: Overwrite(C3, "role", deny=P("connect"))}),
    }
    g.members = {
        OWNER: Member(OWNER, "TestOwner", [ADMIN_ROLE]),
        BOT: Member(BOT, "ServerBot", [BOTROLE], bot=True),
        50: Member(50, "TestUserB", [C2]),
        51: Member(51, "Sara", [C1]),
        52: Member(52, "Ali", [C3]),
        53: Member(53, "Mod", [MOD_ROLE, C1]),
    }
    return g


@pytest.fixture
def cfg() -> ServerConfig:
    return ServerConfig.model_validate({
        "access": {"admin_roles": ["Admin"], "moderator_roles": ["Moderator"]},
        "baseline": {
            "trust_levels": {
                "trust_level_1": {"role": "Level 1", "rank": 1},
                "trust_level_2": {"role": "Level 2", "rank": 2},
                "trust_level_3": {"role": "Level 3", "rank": 3},
            },
            "channels": [
                {"match": "#general", "access": {"trust_level_1": "write", "trust_level_2": "write", "trust_level_3": "write"}},
                {"match": "category:Level 1", "access": {"trust_level_1": "write", "trust_level_2": "none", "trust_level_3": "none",
                                                          "default": "none"}},
                {"match": "#gaming", "access": {"trust_level_1": "write", "trust_level_2": "write", "trust_level_3": "read"}},
            ],
            "hierarchy": ["Admin", "Moderator", "Level 1", "Level 2", "Level 3"],
        },
    })
