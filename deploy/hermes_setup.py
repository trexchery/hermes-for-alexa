"""Create and lock down the dedicated Hermes profile "voice" for the Alexa chain (docs/ARCHITECTURE.md).
Idempotent; prints no secrets; never edits the main Hermes profile (it only reads two provider keys from its .env).

    python -m deploy.hermes_setup [--no-restart]

Layout: HA -> deploy/voice_proxy.py on :8642 (proxy key) -> voice profile api_server on 127.0.0.1:8643 (upstream key).
"""
from __future__ import annotations

import http.server
import json
import os
import re
import secrets as stdlib_secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from deploy import secrets, state

HERMES_HOME = Path(os.environ.get("HERMES_HOME") or Path(os.environ.get("LOCALAPPDATA", "")) / "hermes")
HERMES_CMD = HERMES_HOME / "bin" / "hermes.cmd"
PROFILE = "voice"
VOICE_HOME = HERMES_HOME / "profiles" / PROFILE  # hermes_cli/profiles.py get_profile_dir
VOICE_PORT = 8643  # the voice gateway's api_server, always 127.0.0.1
PROXY_PORT = 8642  # deploy/voice_proxy.py, what HA calls
DOCKER_SUBNETS = "172.16.0.0/12,192.168.65.0/24"
FW_RULE = "hermes-alexa-8642"
# Tools the voice agent may have: web search/extract and the profile's own memory (MEMORY.md/USER.md = the
# shared household memory). no_mcp keeps every MCP server off this platform.
ALLOWED_TOOLSETS = {"web", "memory"}
API_TOOLSETS = ["web", "memory", "no_mcp"]
# agent.disabled_toolsets is applied last by _get_platform_tools and, at tool granularity, to cron agents.
# Every toolset in hermes-agent toolsets.py except web/memory and the composites that contain their tools
# (coding, debugging, safe, search: disabling those would strip web_search as well).
DENIED_TOOLSETS = [
    "bot_room", "browser", "catalog", "clarify", "code_execution", "computer_use", "connections", "context_engine",
    "cronjob", "delegation", "desktop_ui", "discord", "discord_admin", "feishu_doc", "feishu_drive", "file",
    "image_gen", "kanban", "project", "session_search", "setup", "skills", "start_chat", "terminal", "todo", "tts",
    "video", "video_gen", "vision", "x_search", "yuanbao",
]
VOICE_CONFIG = (
    # Own gateway process beside the main one: never multiplexed into it, never served at /p/voice with its key.
    (["gateway", "standalone"], True),
    (["gateway", "api_server", "max_concurrent_runs"], 6),
    (["display", "platforms", "api_server", "interim_assistant_messages"], False),
    (["platform_toolsets", "api_server"], API_TOOLSETS),
    (["platform_toolsets", "cron"], []),  # explicit zero toolsets for any cron job of this profile
    (["agent", "disabled_toolsets"], DENIED_TOOLSETS),
    # The default model (pick a faster one with hermes -p voice model) with thinking off for latency.
    (["agent", "reasoning_effort"], "none"),
    (["model", "reasoning_effort"], "none"),
)
# Credentials the voice profile needs from the main .env (a profile loads only its own .env): the model
# provider and the web backend. Copied, never printed.
COPIED_ENV = ("OPENROUTER_API_KEY", "FIRECRAWL_API_KEY", "FIRECRAWL_API_URL")

# ---------- pure helpers (unit tested) ----------


def replace_env_lines(text: str, updates: dict[str, str]) -> str:
    """Set KEY=value for each key in updates (first occurrence replaced, later duplicates dropped, missing
    keys appended). Every other line, including other API_SERVER_* lines, and the line endings are kept."""
    eol = "\r\n" if "\r\n" in text else "\n"
    lines = text.replace("\r\n", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    out, done = [], set()
    for line in lines:
        m = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if m and m.group(1) in updates:
            if m.group(1) not in done:
                out.append(f"{m.group(1)}={updates[m.group(1)]}")
                done.add(m.group(1))
            continue
        out.append(line)
    out += [f"{k}={v}" for k, v in updates.items() if k not in done]
    return eol.join(out) + eol


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _skip(line: str) -> bool:
    s = line.strip()
    return not s or s.startswith("#")


def _block_end(lines: list[str], i: int, hi: int) -> int:
    """Index after the block that starts at the key line i (list items at the key's indent belong to it)."""
    n = _indent(lines[i])
    j = i + 1
    while j < hi:
        if not _skip(lines[j]) and (_indent(lines[j]) < n or (_indent(lines[j]) == n and not lines[j].lstrip().startswith("- "))):
            break
        j += 1
    while j > i + 1 and _skip(lines[j - 1]):
        j -= 1  # trailing blanks and comments belong to what follows
    return j


def _scalar(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    return v if re.match(r"^[A-Za-z0-9_./:-]+$", v) else json.dumps(v)


def _render(path: list[str], value, indent: int) -> list[str]:
    pad = " " * indent
    if len(path) > 1:
        return [f"{pad}{path[0]}:"] + _render(path[1:], value, indent + 2)
    if isinstance(value, list):
        if not value:
            return [f"{pad}{path[0]}: []"]  # a bare "key:" is null, which Hermes reads as "platform default"
        return [f"{pad}{path[0]}:"] + [f"{pad}  - {_scalar(x)}" for x in value]
    return [f"{pad}{path[0]}: {_scalar(value)}"]


def set_yaml_path(text: str, path: list[str], value) -> str:
    """Targeted edit of block-style YAML: set a nested key to a scalar or list of scalars, leaving every
    other line, comment and the line endings untouched. Raises ValueError on shapes it cannot edit safely."""
    eol = "\r\n" if "\r\n" in text else "\n"
    lines = text.replace("\r\n", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    lo, hi, child = 0, len(lines), 0
    for depth, key in enumerate(path):
        found = next((i for i in range(lo, hi) if not _skip(lines[i]) and _indent(lines[i]) == child
                      and re.match(rf"^{re.escape(key)}\s*:(\s|$)", lines[i].lstrip())), None)
        if found is None:
            at = hi
            while at > lo and _skip(lines[at - 1]):
                at -= 1
            lines[at:at] = _render(path[depth:], value, child)
            break
        end = _block_end(lines, found, hi)
        if depth == len(path) - 1:
            lines[found:end] = _render(path[depth:], value, child)
            break
        if lines[found].split(":", 1)[1].split("#")[0].strip():
            raise ValueError(f"cannot edit inside inline value of '{key}'")
        lo, hi = found + 1, end
        child = next((_indent(lines[i]) for i in range(lo, hi) if not _skip(lines[i])), child + 2)
    return eol.join(lines) + eol


def env_values(text: str, names: tuple[str, ...]) -> dict[str, str]:
    """KEY -> raw value (as written, quotes kept) for the named keys present with a non-empty value."""
    out: dict[str, str] = {}
    for line in text.replace("\r\n", "\n").split("\n"):
        m = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$", line)
        if m and m.group(1) in names and m.group(2).strip() and m.group(1) not in out:
            out[m.group(1)] = m.group(2).strip()
    return out


def voice_config(text: str) -> str:
    for path, value in VOICE_CONFIG:
        text = set_yaml_path(text, path, value)
    return text


def toolset_violations(names: object) -> list[str]:
    """Names outside ALLOWED_TOOLSETS in the toolsets Hermes resolves for the api_server platform;
    ["<unreadable>"] if that is not a list of names."""
    if not isinstance(names, list):
        return ["<unreadable>"]
    return sorted(str(n) for n in names if n not in ALLOWED_TOOLSETS)


# ---------- side effects ----------


def _backup(path: Path) -> None:
    shutil.copy2(path, path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}"))


def docker_bind_ip() -> str | None:
    """IPv4 of the Docker/WSL vEthernet adapter (what containers reach as the host), or None."""
    r = subprocess.run(["powershell", "-NoProfile", "-Command",
                        "(Get-NetIPAddress -AddressFamily IPv4 | Where-Object InterfaceAlias -like 'vEthernet*' "
                        "| Select-Object -First 1).IPAddress"], capture_output=True, text=True, timeout=30)
    ip = r.stdout.strip()
    return ip if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", ip) else None


def _read(path: Path) -> str:
    return path.read_bytes().decode("utf-8") if path.exists() else ""


def _write_if_changed(path: Path, old: str, new: str, backup: bool = True) -> bool:
    if new == old:
        return False
    if backup and path.exists():
        _backup(path)
    path.write_bytes(new.encode("utf-8"))
    return True


def docker_reaches_host_loopback() -> bool:
    """Start a throwaway server on 127.0.0.1 and see if a container reaches it via host.docker.internal."""

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        r = subprocess.run(["docker", "run", "--rm", "--add-host", "host.docker.internal:host-gateway", "curlimages/curl",
                            "-fsS", "-m", "8", f"http://host.docker.internal:{srv.server_port}/"],
                           capture_output=True, text=True, timeout=180)
        return r.returncode == 0 and r.stdout.strip() == "ok"
    finally:
        srv.shutdown()


def ensure_firewall_rule() -> bool:
    show = subprocess.run(["netsh", "advfirewall", "firewall", "show", "rule", f"name={FW_RULE}"], capture_output=True, text=True)
    if show.returncode == 0:
        return True
    add = subprocess.run(["netsh", "advfirewall", "firewall", "add", "rule", f"name={FW_RULE}", "dir=in", "action=allow",
                          "protocol=TCP", f"localport={PROXY_PORT}", f"remoteip={DOCKER_SUBNETS}", "profile=any"],
                         capture_output=True, text=True)
    if add.returncode != 0:
        print("  firewall rule needs an elevated shell. Run once as Administrator:")
        print(f"  netsh advfirewall firewall add rule name={FW_RULE} dir=in action=allow protocol=TCP localport={PROXY_PORT} remoteip={DOCKER_SUBNETS} profile=any")
    return add.returncode == 0


def hermes(*args: str, timeout: int = 180) -> subprocess.CompletedProcess:
    # Non-interactive: `gateway start` must never install a login task or prompt (hermes_cli/gateway_windows.py).
    env = {**os.environ, "HERMES_GATEWAY_INSTALL_START_ON_LOGIN": "0"}
    return subprocess.run([str(HERMES_CMD), *args], capture_output=True, text=True, timeout=timeout, env=env)


def ensure_profile() -> None:
    if (VOICE_HOME / "config.yaml").exists():
        return
    # Fresh, not a clone: no personal memory, SOUL, skills, plugins, MCP servers or messaging channels.
    r = hermes("profile", "create", PROFILE, "--no-alias", "--no-skills")
    if r.returncode != 0 or not VOICE_HOME.is_dir():
        raise SystemExit(f"hermes profile create {PROFILE} failed ({r.returncode}): {r.stderr.strip()[-400:]}")
    print(f"  created Hermes profile '{PROFILE}' at {VOICE_HOME}")


def _key(name: str) -> str:
    if not secrets.exists(name):
        secrets.put(name, stdlib_secrets.token_hex(32))
    return secrets.get(name)


def apply_config() -> bool:
    """Write the voice profile's .env and config.yaml; True when anything changed."""
    client, upstream = _key("hermes_api_key"), _key("hermes_voice_upstream_key")  # HA's key, the gateway's key
    if client == upstream:
        raise SystemExit("hermes_api_key and hermes_voice_upstream_key must differ: delete one and rerun")
    copied = env_values(_read(HERMES_HOME / ".env"), COPIED_ENV)
    if "OPENROUTER_API_KEY" not in copied:
        print("  OPENROUTER_API_KEY is not in the main .env (it may live in auth.json): "
              f"log the profile in once with `hermes -p {PROFILE} model`")
    env_path = VOICE_HOME / ".env"
    env_text = _read(env_path)
    # No backup: the .env is regenerated here, and backups would carry the keys into `hermes profile export`.
    changed = _write_if_changed(env_path, env_text, replace_env_lines(env_text, {
        **copied, "API_SERVER_ENABLED": "true", "API_SERVER_KEY": upstream,
        "API_SERVER_HOST": "127.0.0.1", "API_SERVER_PORT": str(VOICE_PORT)}), backup=False)
    cfg_path = VOICE_HOME / "config.yaml"
    cfg = _read(cfg_path)
    changed |= _write_if_changed(cfg_path, cfg, voice_config(cfg))
    return changed


def _get(path: str, key: str | None = None, timeout: int = 5) -> tuple[int, object]:
    req = urllib.request.Request(f"http://127.0.0.1:{VOICE_PORT}{path}",
                                 headers={"Authorization": f"Bearer {key}"} if key else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, None
    except (OSError, ValueError):
        return 0, None


def health_ok(timeout: int = 120) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if _get("/health")[0] == 200:
            return True
        time.sleep(3)
    return False


# Hermes' own resolver for what the api_server agent gets (MCP and platform extras included), run under the voice
# HERMES_HOME. It needs the hermes-agent venv interpreter (override with env HERMES_PYTHON).
RESOLVER = ("import json;from hermes_cli.config import load_config;from hermes_cli.tools_config import "
            "_get_platform_tools as g;print(json.dumps(sorted(g(load_config(),'api_server'))))")


def verify_toolsets() -> list[str]:
    agent = HERMES_HOME / "hermes-agent"
    py = os.environ.get("HERMES_PYTHON") or str(agent / "venv" / "Scripts" / "python.exe")
    try:
        r = subprocess.run([py, "-c", RESOLVER], cwd=agent, capture_output=True,
                           text=True, timeout=120, env={**os.environ, "HERMES_HOME": str(VOICE_HOME)})
        names = json.loads(r.stdout.strip().splitlines()[-1]) if r.returncode == 0 else None
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        names = None
    if names is None:
        print(f"  could not run the toolset check with {py}; set env HERMES_PYTHON to the hermes-agent venv python.exe")
    return toolset_violations(names)


def main(argv: list[str]) -> int:
    if not (HERMES_HOME / "config.yaml").exists() or not HERMES_CMD.exists():
        print(f"Hermes not found at {HERMES_HOME}")
        return 1
    print("checking whether Docker containers reach host loopback ...")
    loopback = docker_reaches_host_loopback()
    host = "127.0.0.1" if loopback else docker_bind_ip()
    if host is None:
        print("  containers do not reach host loopback and no vEthernet adapter was found: not binding 0.0.0.0")
        return 1
    print(f"  {'yes' if loopback else 'no'}: the voice proxy binds {host}:{PROXY_PORT}"
          + ("" if loopback else " (port restricted to the Docker subnets)"))
    if not loopback and not ensure_firewall_rule():
        return 1
    state.update(voice_proxy_host=host)
    ensure_profile()
    changed = apply_config()
    print(f"profile '{PROFILE}' {'updated (config.yaml backup kept)' if changed else 'already configured'}; "
          "keys in DPAPI as hermes_api_key (HA) and hermes_voice_upstream_key (gateway)")
    if "--no-restart" in argv:
        return 0
    if changed:
        hermes("-p", PROFILE, "gateway", "stop")  # nothing to stop on the first run; the result is not needed
    hermes("-p", PROFILE, "gateway", "start")
    if not health_ok():
        print(f"voice gateway did not answer http://127.0.0.1:{VOICE_PORT}/health in 120 s; see {VOICE_HOME / 'logs'}")
        return 1
    bad = verify_toolsets()
    if bad:
        # Disable the api_server first: the watchdog restarts a stopped gateway, which must then serve nothing.
        env_path = VOICE_HOME / ".env"
        _write_if_changed(env_path, _read(env_path), replace_env_lines(_read(env_path), {"API_SERVER_ENABLED": "false"}), backup=False)
        hermes("-p", PROFILE, "gateway", "stop")
        print(f"voice gateway STOPPED: toolsets outside {sorted(ALLOWED_TOOLSETS)} are enabled: {', '.join(bad)}")
        return 1
    print(f"voice gateway healthy on 127.0.0.1:{VOICE_PORT}; enabled toolsets limited to {sorted(ALLOWED_TOOLSETS)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
