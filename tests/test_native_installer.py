"""Native (Docker-free) manager: .env handling, invite link, intent check, update safety helpers."""
import importlib.util
from pathlib import Path

import pytest

VRB = Path(__file__).resolve().parent.parent / "installer" / "vrb.py"
if not VRB.exists():   # the Docker image ships only the bot; the Windows manager lives in the release folder
    pytest.skip("installer not part of this install", allow_module_level=True)
spec = importlib.util.spec_from_file_location("vrb", VRB)
vrb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vrb)


def test_env_roundtrip_keeps_comments(tmp_path):
    env = tmp_path / ".env"
    env.write_text("\n".join(["# keep me", "DISCORD_TOKEN=", "LAVALINK_PASSWORD=change-me",
                              "# DISCORD_TOKEN=change-me-random", ""]))
    vrb.set_env_value("DISCORD_TOKEN", "your-token-here", env)
    vrb.set_env_value("NEW_KEY", "1", env)
    text = env.read_text()
    assert "# keep me" in text and "# DISCORD_TOKEN=change-me-random" in text
    assert vrb.read_env(env) == {"DISCORD_TOKEN": "your-token-here", "LAVALINK_PASSWORD": "change-me", "NEW_KEY": "1"}


def test_invite_and_intent():
    url = vrb.invite_url("123")
    assert "client_id=123" in url and f"permissions={vrb.INVITE_PERMISSIONS}" in url and "applications.commands" in url
    assert vrb.members_intent_on({"flags": 1 << 15}) and vrb.members_intent_on({"flags": 1 << 14})
    assert not vrb.members_intent_on({"flags": 1 << 19}) and not vrb.members_intent_on({})


def test_process_roles():
    assert vrb.role({"name": "pythonw.exe", "cmd": '"C:/b/runtime/python/pythonw.exe" "C:/b/installer/vrb.py" supervise'}) == "supervisor"
    assert vrb.role({"name": "python.exe", "cmd": '"C:/b/runtime/python/python.exe" -m vrbot'}) == "bot"
    assert vrb.role({"name": "java.exe", "cmd": '"C:/b/runtime/java/bin/java.exe" -jar Lavalink.jar'}) == "lavalink"
    assert vrb.role({"name": "python.exe", "cmd": '"C:/b/runtime/python/python.exe" "C:/b/installer/vrb.py" status'}) is None


def test_versions():
    assert vrb.version_tuple("v1.10.0") > vrb.version_tuple("1.9.9") and vrb.version_tuple("x") == (0,)


def test_update_swap_never_touches_user_data(tmp_path, monkeypatch):
    root, new = tmp_path / "bot", tmp_path / "new"
    for d in ("data", "runtime", "logs", "vrbot"):
        (root / d).mkdir(parents=True)
    (root / ".env").write_text("DISCORD_TOKEN=your-token-here")
    (root / "data" / "vrbot.db").write_text("db")
    (root / "VERSION").write_text("1.0.0")
    (new / "vrbot").mkdir(parents=True)
    (new / "VERSION").write_text("1.1.0")
    monkeypatch.setattr(vrb, "ROOT", root)
    vrb.swap_code(new, root / "previous" / "1.0.0")
    assert (root / "VERSION").read_text() == "1.1.0" and (root / "previous" / "1.0.0" / "VERSION").read_text() == "1.0.0"
    assert (root / ".env").read_text() == "DISCORD_TOKEN=your-token-here" and (root / "data" / "vrbot.db").exists()
    assert (root / "runtime").is_dir() and (root / "logs").is_dir()


def test_native_env_overrides(tmp_path, monkeypatch):
    monkeypatch.setattr(vrb, "ENV", tmp_path / ".env")
    (tmp_path / ".env").write_text("DATA_DIR=/data\nCOMPOSE_PROFILES=\n")
    env = vrb.bot_env()
    assert env["DATA_DIR"] == str(vrb.DATA) and env["CONFIG_PATH"].endswith("server.yaml")
    assert "LAVALINK_URI" not in env            # music off -> no Lavalink address
