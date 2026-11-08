r"""Non-secret deploy settings and state in %LOCALAPPDATA%\hermes-alexa\state.json.

Settings (set once, `install.ps1 -Hostname ...` or `python -m deploy.state set key=value`):
  public_hostname  public DNS name of the tunnel, e.g. ha-voice.example.com (required)
  privacy_url      privacy policy URL the skill manifest demands (required by skill_deploy)
  cf_tunnel_name   Cloudflare tunnel name        (default hermes-ha)
  aws_region       region of Lambda, SSM, IAM    (default us-east-1)
  timezone         IANA zone for Home Assistant  (default UTC)
State written by the deploy scripts: skill_id, lambda_arn, voice_proxy_host, cf_service_token_id.
"""
from __future__ import annotations

import json
import sys

from deploy.secrets import app_dir

DEFAULTS = {"cf_tunnel_name": "hermes-ha", "aws_region": "us-east-1", "timezone": "UTC"}
SETTINGS = ("public_hostname", "privacy_url", *DEFAULTS)


def load() -> dict:
    p = app_dir() / "state.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def update(**kv) -> dict:
    s = {**load(), **kv}
    p = app_dir() / "state.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(s, indent=2), encoding="utf-8")
    return s


def get(key: str) -> str:
    """A setting, its default, or a SystemExit that says how to set it."""
    v = load().get(key) or DEFAULTS.get(key)
    if not v:
        raise SystemExit(f"Setting '{key}' is missing. Set it once:  python -m deploy.state set {key}=VALUE   (or install.ps1 parameters)")
    return v


def main(argv: list[str]) -> int:
    if argv[:1] == ["set"] and len(argv) > 1:
        kv = dict(a.split("=", 1) for a in argv[1:] if "=" in a)
        if kv.keys() - set(SETTINGS):
            print("unknown setting; known:", ", ".join(SETTINGS))
            return 1
        update(**{k: v for k, v in kv.items() if v})
        return 0
    if argv[:1] == ["check"]:
        for k in argv[1:]:
            get(k)
        return 0
    print("usage: python -m deploy.state set key=value ... | check key ...")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
