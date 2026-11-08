import pytest

from deploy import state


def test_settings_default_or_fail_clearly(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_ALEXA_HOME", str(tmp_path))
    assert state.get("timezone") == "UTC" and state.get("cf_tunnel_name") == "hermes-ha"
    with pytest.raises(SystemExit, match="public_hostname"):
        state.get("public_hostname")
    assert state.main(["set", "public_hostname=ha.example.com"]) == 0
    assert state.get("public_hostname") == "ha.example.com"
    assert state.main(["set", "bogus=1"]) == 1
