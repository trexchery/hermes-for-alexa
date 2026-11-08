"""Optional: lock the public hostname (setting public_hostname) behind a Cloudflare Access service token that only the Lambda holds.

    python -m deploy.set_secret cf_api_token     (token needs: Access Apps and Policies Edit, Access Service Tokens Edit, Zone Read)
    python -m deploy.cloudflare_access
Without cf_api_token this prints how to add it and exits 0 (the chain still works; the HA bearer token remains).
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

from deploy import secrets, state

API = "https://api.cloudflare.com/client/v4"
TOKEN_NAME = "hermes-alexa-lambda"
APP_NAME = "Hermes Alexa voice"
POLICY_NAME = "Lambda service token"


def app_payload(hostname: str) -> dict:
    return {"name": APP_NAME, "domain": hostname, "type": "self_hosted", "session_duration": "24h"}


def policy_payload(token_id: str) -> dict:
    """Service Auth: only the named service token passes; no allow-everyone rule."""
    return {"name": POLICY_NAME, "decision": "non_identity", "include": [{"service_token": {"token_id": token_id}}], "precedence": 1}


def cf(method: str, path: str, token: str, body: dict | None = None):
    req = urllib.request.Request(API + path, method=method, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)["result"]
    except urllib.error.HTTPError as e:
        raise SystemExit(f"Cloudflare API {method} {path} failed ({e.code}); check the token permissions")


def main() -> int:
    if not secrets.exists("cf_api_token"):
        print("Cloudflare Access skipped: no cf_api_token. To enable, create an API token and run:  python -m deploy.set_secret cf_api_token")
        return 0
    tok = secrets.get("cf_api_token")
    hostname = state.get("public_hostname")
    zone = cf("GET", f"/zones?name={hostname.split('.', 1)[1]}", tok)
    if not zone:
        raise SystemExit("zone not visible to this token")
    acct = zone[0]["account"]["id"]
    st = state.load()
    token_id = st.get("cf_service_token_id")
    if not (token_id and secrets.exists("cf_access_client_id")):
        t = cf("POST", f"/accounts/{acct}/access/service_tokens", tok, {"name": TOKEN_NAME, "duration": "8760h"})
        secrets.put("cf_access_client_id", t["client_id"])
        secrets.put("cf_access_client_secret", t["client_secret"])  # shown by Cloudflare only once
        token_id = t["id"]
        state.update(cf_service_token_id=token_id)
        print("  service token created and stored")
    app = next((a for a in cf("GET", f"/accounts/{acct}/access/apps", tok) if a.get("domain") == hostname), None)
    if app is None:
        app = cf("POST", f"/accounts/{acct}/access/apps", tok, app_payload(hostname))
        print("  access application created")
    pols = cf("GET", f"/accounts/{acct}/access/apps/{app['id']}/policies", tok)
    if not any(p["name"] == POLICY_NAME for p in pols):
        cf("POST", f"/accounts/{acct}/access/apps/{app['id']}/policies", tok, policy_payload(token_id))
        print("  service auth policy created")
    print("Cloudflare Access ready. Re-run aws_deploy so the Lambda gets the service token.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
