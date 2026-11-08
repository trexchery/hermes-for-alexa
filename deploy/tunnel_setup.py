"""Create the Cloudflare tunnel (setting cf_tunnel_name, default hermes-ha), its DNS route and the container config. Idempotent.

    python -m deploy.tunnel_setup
Never touches other tunnels or their credential files. Requires `cloudflared tunnel login` once (cert.pem).
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from deploy import state

PATH_RE = r"^/api/hermes_voice/alexa/v1/turn$"
CONFIG_DIR = Path.home() / ".hermes-tunnel"
CF_DIR = Path.home() / ".cloudflared"


def render_config(tunnel_id: str, hostname: str) -> str:
    """Config read inside the cloudflared container (deploy/ha/compose.yaml mounts it and the credentials JSON).
    Only the Alexa turn endpoint is exposed; everything else is a 404 at the edge of the tunnel."""
    return (
        f"tunnel: {tunnel_id}\n"
        "credentials-file: /etc/cloudflared/credentials.json\n"
        "metrics: 0.0.0.0:20251\n"
        "ingress:\n"
        f"  - hostname: {hostname}\n"
        f"    path: {PATH_RE}\n"
        "    service: http://homeassistant:8123\n"
        "  - service: http_status:404\n"
    )


def _cf() -> str:
    exe = shutil.which("cloudflared") or r"C:\Program Files (x86)\cloudflared\cloudflared.exe"
    if not Path(exe).exists():
        raise SystemExit("cloudflared not found (winget install Cloudflare.cloudflared)")
    return exe


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([_cf(), *args], capture_output=True, text=True, timeout=120)


def _live(t: dict) -> bool:
    """cloudflared reports live tunnels with deleted_at "0001-01-01T00:00:00Z" (Go zero time)."""
    d = t.get("deleted_at") or ""
    return not d or d.startswith("0001-")


def find_tunnel(name: str) -> str | None:
    r = _run("tunnel", "list", "--name", name, "--output", "json")
    if r.returncode != 0:
        raise SystemExit("cloudflared tunnel list failed; run once:  cloudflared tunnel login")
    return next((t["id"] for t in (json.loads(r.stdout or "[]") or []) if t["name"] == name and _live(t)), None)


def main() -> int:
    name, hostname = state.get("cf_tunnel_name"), state.get("public_hostname")
    if not (CF_DIR / "cert.pem").exists():
        print("cloudflared is not logged in. Run once:  cloudflared tunnel login")
        return 1
    tid = find_tunnel(name)
    if tid is None:
        r = _run("tunnel", "create", name)
        if r.returncode != 0:
            print("tunnel create failed:", r.stderr.strip()[-300:])
            return 1
        tid = find_tunnel(name)
        print(f"created tunnel {name}")
    else:
        print(f"tunnel {name} exists")
    r = _run("tunnel", "route", "dns", name, hostname)
    if r.returncode != 0 and "already exists" not in (r.stderr + r.stdout):
        print("dns route failed:", r.stderr.strip()[-300:])
        return 1
    creds = CF_DIR / f"{tid}.json"
    if not creds.exists():
        print(f"credentials file {creds.name} missing (tunnel created on another machine?)")
        return 1
    CONFIG_DIR.mkdir(exist_ok=True)
    cfg = CONFIG_DIR / "config.yml"
    new = render_config(tid, hostname)
    if not cfg.exists() or cfg.read_text(encoding="utf-8") != new:
        cfg.write_text(new, encoding="utf-8", newline="\n")  # read by Linux cloudflared
    (Path(__file__).parent / "ha" / ".env").write_text(f"HERMES_TUNNEL_ID={tid}\nHA_TZ={state.get('timezone')}\n", encoding="utf-8")  # not secrets; compose reads it
    print(f"container config at {cfg}; hostname https://{hostname}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
