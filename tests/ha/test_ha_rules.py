"""S4, S5, D2, D3 rules (pure)."""

from datetime import datetime

import pytest

from hvload import load

rules = load("rules")


@pytest.mark.parametrize(
    "url",
    [
        "https://hermes.example.com/v1",
        "HTTPS://hermes.example.com:8642",
        "http://localhost:8642/v1",
        "http://127.0.0.1:8642/v1",
        "http://[::1]:8642/v1",
        "http://host.docker.internal:8642/v1",
        "http://10.1.2.3:8642",
        "http://172.16.0.9:8642",
        "http://192.168.1.20:8642/p/voice/v1",
        "http://[fd00::5]:8642",
    ],
)
def test_s4_allowed(url):
    assert rules.base_url_allowed(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://hermes.example.com/v1",
        "http://8.8.8.8:8642",
        "http://172.32.0.1:8642",  # just outside 172.16/12
        "http://0.0.0.0:8642",
        "http://hermes.local:8642",
        "http://host.docker.internal.evil.com",
        "http://localhost.evil.com",
        "ftp://10.0.0.1",
        "10.0.0.1:8642",
        "http://",
        "",
    ],
)
def test_s4_refused(url):
    assert not rules.base_url_allowed(url)


def test_s5_missing_user_or_device_gets_a_scope_per_conversation():
    a = rules.render_user_key("{user_id}/{device_id}", None, None, "conv-a")
    b = rules.render_user_key("{user_id}/{device_id}", None, None, "conv-b")
    assert a != b
    assert "anonymous" not in a and "nodevice" not in a
    assert rules.render_user_key("{user_id}", "u1", None, "c") == "u1"
    assert rules.render_user_key("", None, None, "c") is None
    assert len(rules.render_user_key("x" * 999, None, None, "c")) == 256


def test_d2_session_ids_are_namespaced_by_user():
    a = rules.conversation_session_id("conv1", "user-a")
    b = rules.conversation_session_id("conv1", "user-b")
    assert a != b and a.startswith("ha-u") and a.endswith("-conv1")
    assert "user-a" not in a  # the id itself is hashed
    assert rules.conversation_session_id("conv1", None) == "ha-nouser-conv1"
    assert rules.conversation_session_id("a/b c", "u").endswith("-a_b_c")


def test_d3_prune_windows_drops_only_idle_ones():
    windows = {"old": {"start": 0.0, "last": 100.0}, "fresh": {"start": 0.0, "last": 990.0}}
    rules.prune_windows(windows, now=1000.0, idle_seconds=60)
    assert list(windows) == ["fresh"]


def test_time_context_formats_local_time():
    assert rules.time_context(datetime(2026, 11, 8, 15, 5)).startswith(
        "Current local date and time: Sunday, November 8, 2026, 3:05 PM"
    )
