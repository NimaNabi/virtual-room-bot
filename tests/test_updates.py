"""Update notifications: version logic, no network without the owner's own token, requests for the host updater."""
import json

from vrbot.cogs.updates import Updates, is_newer, kind_of, parse_version


def test_semver():
    assert parse_version("v1.2.3") == (1, 2, 3) and parse_version("x") is None
    assert is_newer("1.0.1", "1.0.0") and not is_newer("1.0.0", "1.0.0") and not is_newer(None, "1.0.0")
    assert kind_of("1.0.1", "1.0.0") == "bug-fix" and kind_of("1.1.0", "1.0.9") == "feature"
    assert kind_of("2.0.0", "1.9.9").startswith("major")


class _Bot:
    pass


async def test_no_network_without_owner_token(monkeypatch, tmp_path):
    monkeypatch.delenv("UPDATE_GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    assert await Updates(_Bot()).check_github() is None


def test_summary_and_request(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "update-check.json").write_text(json.dumps({"version": "99.0.0", "notes": ["Fixed X"], "breaking": False}))
    (tmp_path / "update-state.json").write_text(json.dumps({"previous": "0.9.0", "last_success": 1700000000}))
    up = Updates(_Bot())
    emb, newer = up.summary()
    assert newer and "Update available" in emb.description and "0.9.0" in emb.description
    assert "Fixed X" in up.notes()
    up.request("99.0.0", 7)
    assert up.pending_request()["version"] == "99.0.0"
