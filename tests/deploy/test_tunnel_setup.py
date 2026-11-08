from pathlib import Path

from deploy.tunnel_setup import _live, render_config


def test_ingress_exposes_only_the_turn_endpoint():
    cfg = render_config("abc-123", "ha.example.com")
    assert "tunnel: abc-123" in cfg and "metrics: 0.0.0.0:20251" in cfg
    assert "credentials-file: /etc/cloudflared/credentials.json" in cfg
    assert "path: ^/api/hermes_voice/alexa/v1/turn$" in cfg
    assert "service: http://homeassistant:8123" in cfg
    assert cfg.rstrip().endswith("- service: http_status:404")  # catch-all last
    assert cfg.count("service:") == 2 and "hostname: ha.example.com" in cfg


def test_live_tunnel_has_go_zero_deleted_at():
    assert _live({"name": "x", "deleted_at": "0001-01-01T00:00:00Z"})
    assert _live({"name": "x"})
    assert not _live({"name": "x", "deleted_at": "2026-01-02T03:04:05Z"})


def test_onboarding_404_means_all_steps_done():
    from deploy.bootstrap_ha import onboarding_steps

    assert all(onboarding_steps(None).values())
    assert onboarding_steps([{"step": "user", "done": False}]) == {"user": False}


def test_find_tunnel_handles_null_list(monkeypatch):
    """cloudflared prints `null` (not `[]`) when no tunnel matches."""
    import subprocess
    from deploy import tunnel_setup
    monkeypatch.setattr(tunnel_setup, "_run", lambda *a: subprocess.CompletedProcess(a, 0, "null", ""))
    assert tunnel_setup.find_tunnel("t") is None


def test_ha_trusts_exactly_the_cloudflared_container_address():
    import re
    from deploy.bootstrap_ha import HTTP_WANT, http_config_ok
    compose = (Path(__file__).parents[2] / "deploy" / "ha" / "compose.yaml").read_text(encoding="utf-8")
    ip = re.search(r"ipv4_address: (\S+)", compose).group(1)
    assert HTTP_WANT["trusted_proxies"] == [ip + "/32"] and HTTP_WANT["use_x_forwarded_for"] is True
    assert http_config_ok({**HTTP_WANT})
    assert not http_config_ok({**HTTP_WANT, "trusted_proxies": [ip + "/32", "172.16.0.0/12"]})  # broader is not ok
