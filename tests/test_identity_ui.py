"""Rendered UI uses the installation's own identity/names — never built-in branding."""
import pytest

from vrbot.cogs.app import ControlCenter
from vrbot.cogs.onboarding import Onboarding
from vrbot.config import ServerConfig


class FakeBot:
    def __init__(self, cfg):
        self.cfg = cfg
        self.guild = None

    def get_cog(self, name):
        return None


def _cfg(menu, names, rules=None):
    data = {"identity": {"control_center_name": menu, "subtitle": "Example Server"}, "trust": {"names": names}}
    if rules:
        data["onboarding"] = {"rules": rules}
    return ServerConfig.model_validate(data)


SETS = [("Hub", {"trust_level_1": "Core", "trust_level_2": "Social", "trust_level_3": "Member"}),
        ("Lounge Menu", {"trust_level_1": "Friends", "trust_level_2": "Members", "trust_level_3": "Guests"})]


@pytest.mark.parametrize("menu,names", SETS)
async def test_control_center_uses_configured_identity(menu, names):
    cc = ControlCenter(FakeBot(_cfg(menu, names)))
    pub = cc.public_embed()
    assert menu in pub.title and "Example Server" in pub.description
    emb, view = await cc.s_home(type("M", (), {"id": 1})())
    assert menu in emb.title


def test_rules_come_from_configuration():
    rules = [["✅", "Custom rule", "Custom text"]]
    emb = Onboarding(FakeBot(_cfg("X", SETS[0][1], rules))).rules_embed()
    assert [f.name for f in emb.fields] == ["✅ Custom rule"]


def test_default_rules_are_generic():
    emb = Onboarding(FakeBot(ServerConfig())).rules_embed()
    text = " ".join(f.name + f.value for f in emb.fields).lower()
    assert "respectful" in text and "friend" not in text


@pytest.mark.parametrize("menu,names", SETS)
def test_no_built_in_branding_in_rendered_text(menu, names):
    cc = ControlCenter(FakeBot(_cfg(menu, names)))
    rendered = (cc.public_embed().title + (cc.public_embed().description or "")).lower()
    for word in ("virtual room", "control center" if menu != "Control Center" else "\0"):
        assert word not in rendered
