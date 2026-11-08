"""Create or update the Alexa skill from skill-package/ with the ask CLI (smapi). Idempotent.

    python -m deploy.skill_deploy [--privacy-url URL]
Needs `ask configure` once and the Lambda ARN in state.json (written by deploy.aws_deploy, first pass).
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from deploy import state

PKG = Path(__file__).resolve().parents[1] / "skill-package"
LOCALE = "en-US"


def fill_manifest(manifest: dict, arn: str, privacy_url: str) -> dict:
    text = json.dumps(manifest).replace("__LAMBDA_ARN__", arn).replace("__PRIVACY_POLICY_URL__", privacy_url)
    return json.loads(text)


def ask_exe() -> str | None:
    exe = shutil.which("ask")
    if not exe and shutil.which("npm"):  # npm's global bin is often missing from PATH on Windows
        prefix = subprocess.run([shutil.which("npm"), "prefix", "-g"], capture_output=True, text=True, timeout=30).stdout.strip()
        exe = shutil.which("ask", path=prefix) if prefix else None
    return exe


def smapi(*args: str):
    exe = ask_exe()
    if not exe:
        raise SystemExit("ask CLI not found (npm i -g ask-cli)")
    r = subprocess.run([exe, "smapi", *args], capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        raise SystemExit(f"ask smapi {args[0]} failed: {(r.stderr or r.stdout).strip()[-400:]}")
    out = r.stdout.strip()
    try:
        return json.loads(out) if out else None
    except ValueError:
        return out


def wait(label: str, fetch, timeout: int = 300) -> None:
    end = time.time() + timeout
    while time.time() < end:
        status = fetch()
        if status == "SUCCEEDED":
            print(f"  {label}: ready")
            return
        if status == "FAILED":
            raise SystemExit(f"{label} failed: ask smapi get-skill-status --skill-id <id>")
        time.sleep(5)
    raise SystemExit(f"{label} did not finish in {timeout} s")


def main(argv: list[str]) -> int:
    if "--privacy-url" in argv:
        state.update(privacy_url=argv[argv.index("--privacy-url") + 1])
    privacy = state.get("privacy_url")  # required by the manifest, unused for a private dev-stage skill
    st = state.load()
    arn = st.get("lambda_arn")
    if not arn:
        print("Lambda ARN unknown: run python -m deploy.aws_deploy first")
        return 1
    try:
        vendors = smapi("get-vendor-list")
        vendor = vendors["vendors"][0]["id"]
    except (SystemExit, KeyError, TypeError, IndexError):
        print("ask CLI is not configured. Run once:  ask configure   (Amazon developer login in the browser)")
        return 1
    manifest = fill_manifest(json.loads((PKG / "skill.json").read_text(encoding="utf-8")), arn, privacy)
    models = sorted((PKG / "interactionModels").glob(f"*/{LOCALE}.json"))
    if not models:
        print("no interaction model in skill-package/interactionModels")
        return 1
    skill_id = st.get("skill_id")
    with tempfile.TemporaryDirectory() as tmp:
        body = Path(tmp) / "manifest.json"
        manifest_status = lambda: (smapi("get-skill-status", "--skill-id", skill_id) or {}).get("manifest", {}).get("lastUpdateRequest", {}).get("status")
        if not skill_id:
            # SMAPI rejects an endpoint whose Lambda does not already trust this skill id, and the id only
            # exists after creation: create without the endpoint, trust the id, then set the endpoint below.
            bare = without_endpoint(manifest["manifest"])
            body.write_text(json.dumps({"vendorId": vendor, "manifest": bare}), encoding="utf-8")
            skill_id = smapi("create-skill-for-vendor", "--manifest", f"file:{body}")["skillId"]
            state.update(skill_id=skill_id)
            print(f"  created skill {skill_id}")
            wait("manifest", manifest_status)
        trust_skill(skill_id)
        body.write_text(json.dumps({"manifest": manifest["manifest"]}), encoding="utf-8")
        smapi("update-skill-manifest", "--skill-id", skill_id, "--stage", "development", "--manifest", f"file:{body}")
        print(f"  updated manifest of skill {skill_id}")
        wait("manifest", manifest_status)
        smapi("set-interaction-model", "--skill-id", skill_id, "--stage", "development", "--locale", LOCALE,
              "--interaction-model", f"file:{models[0]}")
        wait("interaction model", lambda: ((smapi("get-skill-status", "--skill-id", skill_id, "--resource", "interactionModel") or {})
                                           .get("interactionModel", {}).get(LOCALE, {}).get("lastUpdateRequest", {}).get("status")))
    smapi("set-skill-enablement", "--skill-id", skill_id, "--stage", "development")
    print(f"skill {skill_id} enabled for testing in the development stage")
    return 0


def without_endpoint(manifest: dict) -> dict:
    m = json.loads(json.dumps(manifest))
    m.get("apis", {}).get("custom", {}).pop("endpoint", None)
    return m


def trust_skill(skill_id: str) -> None:
    """Let only this skill invoke the Lambda (same permission aws_deploy sets on its second pass)."""
    import boto3
    from deploy import aws_deploy
    aws_deploy.ensure_permission(boto3.client("lambda", region_name=state.get("aws_region")), skill_id)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
