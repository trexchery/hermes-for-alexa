"""Bootstrap a fresh Home Assistant with no browser. Idempotent; prints no secrets.

    python -m deploy.bootstrap_ha            (needs: pip install websockets)

DPAPI names written: ha_admin_password, ha_alexa_password, ha_admin_token (ops), ha_alexa_token (Lambda).
Endpoints verified against home-assistant/core 2026.11.0 (onboarding, auth login_flow, config/auth*, auth/long_lived_access_token).
"""
from __future__ import annotations

import json
import secrets as stdlib_secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from deploy import secrets

BASE = "http://127.0.0.1:8123"
CLIENT_ID = BASE + "/"
REDIRECT = BASE + "/?auth_callback=1"
ADMIN_USER = "hermes-admin"
ALEXA_USER = "alexa"
HERMES_URL = "http://host.docker.internal:8642/v1"


def http(method: str, path: str, body=None, token: str | None = None, form: dict | None = None):
    headers = {}
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, None


def wait_for_ha(timeout: int = 300) -> None:
    end = time.time() + timeout
    while time.time() < end:
        try:
            if http("GET", "/api/onboarding")[0] in (200, 404):  # 404: onboarding finished, HA restarted since
                return
        except OSError:
            pass
        time.sleep(3)
    raise SystemExit("Home Assistant did not come up on 127.0.0.1:8123 in 5 minutes (docker logs homeassistant)")


def exchange(code: str) -> str:
    status, body = http("POST", "/auth/token", form={"grant_type": "authorization_code", "code": code, "client_id": CLIENT_ID})
    if status != 200:
        raise SystemExit(f"token exchange failed ({status})")
    return body["access_token"]


def login(username: str, password: str) -> str:
    status, flow = http("POST", "/auth/login_flow", {"client_id": CLIENT_ID, "handler": ["homeassistant", None], "redirect_uri": REDIRECT})
    if status != 200:
        raise SystemExit(f"login flow start failed ({status})")
    status, res = http("POST", f"/auth/login_flow/{flow['flow_id']}", {"client_id": CLIENT_ID, "username": username, "password": password})
    if status != 200 or res.get("type") != "create_entry":
        raise SystemExit(f"login as {username} failed (wrong or missing password in DPAPI?)")
    return exchange(res["result"])


def ws_call(access: str, *msgs: dict) -> list:
    from websockets.sync.client import connect  # deploy extra

    with connect("ws://127.0.0.1:8123/api/websocket", open_timeout=10) as ws:
        ws.recv()  # auth_required
        ws.send(json.dumps({"type": "auth", "access_token": access}))
        if json.loads(ws.recv()).get("type") != "auth_ok":
            raise SystemExit("websocket auth rejected")
        out = []
        for i, msg in enumerate(msgs, 1):
            ws.send(json.dumps({"id": i, **msg}))
            while True:
                res = json.loads(ws.recv())
                if res.get("id") == i:
                    break
            if not res.get("success"):
                raise SystemExit(f"{msg['type']} failed: {res.get('error', {}).get('code')}")
            out.append(res["result"])
        return out


def token_valid(token: str) -> bool:
    try:
        return http("GET", "/api/", token=token)[0] == 200
    except OSError:
        return False


def onboarding_steps(body: object) -> dict:
    """Step -> done. HA registers no onboarding view once every step is done (404, no list): then all are done."""
    if not isinstance(body, list):
        return {s: True for s in ("user", "core_config", "analytics", "integration")}
    return {s["step"]: s["done"] for s in body}


def ensure_admin() -> str:
    """Return an access token for the admin, creating the admin through onboarding on a fresh install."""
    steps = onboarding_steps(http("GET", "/api/onboarding")[1])
    if not steps.get("user"):
        pw = stdlib_secrets.token_urlsafe(24)
        secrets.put("ha_admin_password", pw)
        status, body = http("POST", "/api/onboarding/users", {"name": "Hermes Admin", "username": ADMIN_USER, "password": pw,
                                                              "client_id": CLIENT_ID, "language": "en"})
        if status != 200:
            raise SystemExit(f"onboarding user step failed ({status})")
        access = exchange(body["auth_code"])
        print("  created admin user")
    else:
        if not secrets.exists("ha_admin_password"):
            raise SystemExit("HA is already onboarded and ha_admin_password is not in DPAPI: "
                             "python -m deploy.set_secret ha_admin_password")
        access = login(ADMIN_USER, secrets.get("ha_admin_password"))
    for step, body in (("core_config", {}), ("analytics", {}), ("integration", {"client_id": CLIENT_ID, "redirect_uri": REDIRECT})):
        if not steps.get(step):
            status, _ = http("POST", f"/api/onboarding/{step}", body, token=access)
            print(f"  onboarding {step}: {'ok' if status == 200 else status}")
    return access


def ensure_alexa_user(admin_access: str) -> None:
    users = ws_call(admin_access, {"type": "config/auth/list"})[0]
    if any(u.get("username") == ALEXA_USER for u in users):
        if not secrets.exists("ha_alexa_password"):
            raise SystemExit("user 'alexa' exists but ha_alexa_password is not in DPAPI: python -m deploy.set_secret ha_alexa_password")
        return
    pw = stdlib_secrets.token_urlsafe(24)
    user = ws_call(admin_access, {"type": "config/auth/create", "name": "Alexa", "group_ids": ["system-users"], "local_only": False})[0]["user"]
    ws_call(admin_access, {"type": "config/auth_provider/homeassistant/create", "user_id": user["id"], "username": ALEXA_USER, "password": pw})
    secrets.put("ha_alexa_password", pw)
    print("  created non-admin user 'alexa'")


def ensure_token(secret_name: str, username: str, client_name: str, admin_access: str | None) -> None:
    if secrets.exists(secret_name) and token_valid(secrets.get(secret_name)):
        print(f"  {secret_name}: present and valid")
        return
    access = admin_access or login(username, secrets.get(f"ha_{username.split('-')[-1]}_password"))
    secrets.put(secret_name, ws_call(access, {"type": "auth/long_lived_access_token", "client_name": client_name, "lifespan": 365})[0])
    print(f"  {secret_name}: created (365 days)")


def ensure_integration(admin_token: str) -> None:
    status, entries = http("GET", "/api/config/config_entries/entry", token=admin_token)
    if status == 200 and any(e.get("domain") == "hermes_voice" for e in entries):
        print("  hermes_voice: already configured")
        return
    if not secrets.exists("hermes_api_key"):
        raise SystemExit("run python -m deploy.hermes_setup first (hermes_api_key missing)")
    status, flow = http("POST", "/api/config/config_entries/flow", {"handler": "hermes_voice"}, token=admin_token)
    if status != 200:
        raise SystemExit(f"hermes_voice flow not available ({status}): restart HA so it loads the custom component (docker restart homeassistant)")
    status, res = http("POST", f"/api/config/config_entries/flow/{flow['flow_id']}",
                       {"base_url": HERMES_URL, "api_key": secrets.get("hermes_api_key")}, token=admin_token)
    if status != 200 or res.get("type") != "create_entry":
        raise SystemExit(f"hermes_voice setup failed: {res.get('errors') if isinstance(res, dict) else status} "
                         "(is Hermes running with api_server enabled?)")
    print("  hermes_voice: configured")


# Only the cloudflared container (fixed address in deploy/ha/compose.yaml) may set X-Forwarded-For.
HTTP_WANT = {"use_x_forwarded_for": True, "trusted_proxies": ["172.30.77.2/32"],
             "ip_ban_enabled": True, "login_attempts_threshold": 5}


def http_config_ok(conf: dict | None) -> bool:
    if not conf:
        return False
    proxies = {p if "/" in p else p + ("/128" if ":" in p else "/32") for p in conf.get("trusted_proxies") or []}
    return all(conf.get(k) == v for k, v in HTTP_WANT.items() if k != "trusted_proxies") and set(HTTP_WANT["trusted_proxies"]) == proxies


def ensure_http_config(admin_access: str) -> str:
    """HA >= 2026.11 keeps http: settings in a store, ignores YAML after a one-time import, and reverts any change
    not promoted within 5 minutes. Stage ours as pending (HA restarts), then promote it to stable."""
    cur = ws_call(admin_access, {"type": "http/config"})[0]
    if http_config_ok(cur["stable"]):
        print("  http reverse-proxy config: confirmed")
        return admin_access
    pending = cur.get("pending")
    if not (http_config_ok(pending) and not pending.get("error") and cur.get("active_config_type") == "pending"):
        ws_call(admin_access, {"type": "http/config/configure", "config": {
            **{k: v for k, v in cur["stable"].items() if k not in ("created_at", "error", "error_message")}, **HTTP_WANT}})
        print("  http reverse-proxy config staged; Home Assistant is restarting ...")
        time.sleep(10)
        wait_for_ha()
        admin_access = login(ADMIN_USER, secrets.get("ha_admin_password"))
    ws_call(admin_access, {"type": "http/config/promote"})
    print("  http reverse-proxy config: promoted to stable")
    return admin_access


def main() -> int:
    print("waiting for Home Assistant ...")
    wait_for_ha()
    admin_access = ensure_http_config(ensure_admin())
    ensure_alexa_user(admin_access)
    ensure_token("ha_admin_token", ADMIN_USER, "ops", admin_access)
    ensure_token("ha_alexa_token", ALEXA_USER, "alexa-lambda", None)
    ensure_integration(secrets.get("ha_admin_token"))
    print("Home Assistant bootstrap complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
