import sys

import pytest

from deploy import secrets


def test_name_is_validated(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_ALEXA_HOME", str(tmp_path))
    for bad in ("../x", "A", "a b", "", "x" * 65):
        with pytest.raises(ValueError):
            secrets.exists(bad)


@pytest.mark.skipif(sys.platform == "win32", reason="non-Windows refusal")
def test_refuses_off_windows(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_ALEXA_HOME", str(tmp_path))
    with pytest.raises(RuntimeError):
        secrets.put("x", "y")


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows only")
def test_roundtrip_is_encrypted(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_ALEXA_HOME", str(tmp_path))
    assert not secrets.exists("tok")
    secrets.put("tok", "s3cret-value")
    assert secrets.exists("tok") and secrets.get("tok") == "s3cret-value"
    assert b"s3cret-value" not in (tmp_path / "secrets" / "tok.bin").read_bytes()
    with pytest.raises(KeyError):
        secrets.get("missing")
