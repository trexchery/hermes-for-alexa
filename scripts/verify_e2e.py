"""End-to-end check of the voice chain, hop by hop, with latency. Exit code 0 only if every hop passes.

    python scripts/verify_e2e.py [--skip-lambda] [--skip-simulate]
Hops: (a) HA bridge on loopback  (b) the same through the public tunnel  (c) the deployed Lambda
(d) a real Alexa NLU round trip with `ask smapi simulate-skill`. Prints no secrets.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(1, str(Path(__file__).resolve().parents[1] / "src"))
from deploy import secrets, state  # noqa: E402
from deploy.aws_deploy import FUNCTION  # noqa: E402
from hermes_alexa_skill.handler import ERROR_SPEECH, SAFE_ERROR, USER_AGENT  # noqa: E402

ERRORS = (SAFE_ERROR, *ERROR_SPEECH.values())

LOCAL = "http://127.0.0.1:8123/api/hermes_voice/alexa/v1/turn"
PATH = "/api/hermes_voice/alexa/v1/turn"
DEVICE = "amzn1.ask.device.VERIFY-E2E"
UTTERANCE = "what time is it"
SIM_UTTERANCE = "ask hermes tell me what time it is"  # matches a HermesQueryIntent carrier sample


def turn_body() -> dict:
    return {"text": UTTERANCE, "kind": "utterance", "alexa_device_id": DEVICE, "alexa_user_id": "amzn1.ask.account.VERIFY-E2E",
            "alexa_session_id": "verify-" + uuid.uuid4().hex[:12], "budget_ms": 5500}


def alexa_envelope(skill_id: str) -> dict:
    sid = "amzn1.echo-api.session." + uuid.uuid4().hex
    return {"version": "1.0",
            "session": {"new": True, "sessionId": sid, "application": {"applicationId": skill_id}, "user": {"userId": "amzn1.ask.account.VERIFY-E2E"}},
            "context": {"System": {"application": {"applicationId": skill_id}, "user": {"userId": "amzn1.ask.account.VERIFY-E2E"},
                                   "device": {"deviceId": DEVICE, "supportedInterfaces": {}}}},
            "request": {"type": "IntentRequest", "requestId": "amzn1.echo-api.request." + uuid.uuid4().hex, "locale": "en-US",
                        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "intent": {"name": "HermesQueryIntent", "confirmationStatus": "NONE",
                                   "slots": {"query": {"name": "query", "value": UTTERANCE, "confirmationStatus": "NONE"}}}}}


def post(url: str, body: dict, headers: dict) -> tuple[int, dict | None]:
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json", "User-Agent": USER_AGENT, **headers}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, None
    except (OSError, ValueError):
        return 0, None


def check_turn(label: str, url: str, headers: dict) -> bool:
    t = time.perf_counter()
    status, body = post(url, turn_body(), headers)
    ms = int((time.perf_counter() - t) * 1000)
    ok = status == 200 and bool(body) and body.get("status") in ("answered", "pending") and bool(body.get("speech"))
    print(f"[{'ok' if ok else 'FAIL'}] {label}: HTTP {status}, {ms} ms" + (f", status={body.get('status')}, source={body.get('source')}" if body else ""))
    return ok


def check_lambda(skill_id: str) -> bool:
    try:
        import boto3
    except ImportError:
        print("[FAIL] lambda: boto3 missing (pip install boto3)")
        return False
    t = time.perf_counter()
    try:
        res = boto3.client("lambda", region_name=state.get("aws_region")).invoke(FunctionName=FUNCTION, Payload=json.dumps(alexa_envelope(skill_id)).encode())
        payload = json.load(res["Payload"])
    except Exception as e:  # noqa: BLE001 - report any AWS failure as a failed hop
        print(f"[FAIL] lambda: {type(e).__name__}")
        return False
    ms = int((time.perf_counter() - t) * 1000)
    resp = payload.get("response", {}) if isinstance(payload, dict) else {}
    speech = (resp.get("outputSpeech") or {}).get("ssml") or (resp.get("outputSpeech") or {}).get("text") or ""
    ok = (res["StatusCode"] == 200 and "FunctionError" not in res and payload.get("version") == "1.0" and bool(speech)
          and not any(e in speech for e in ERRORS))  # the handler speaks an error string when it could not reach HA
    print(f"[{'ok' if ok else 'FAIL'}] lambda invoke: {ms} ms" + ("" if ok else f", FunctionError={res.get('FunctionError')}"))
    return ok


def check_simulate(skill_id: str) -> bool:
    from deploy.skill_deploy import ask_exe
    ask = ask_exe()
    if not ask:
        print("[FAIL] simulate: ask CLI not found")
        return False

    def run(*a: str):
        r = subprocess.run([ask, "smapi", *a], capture_output=True, text=True, timeout=120)
        return json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else None

    t = time.perf_counter()
    sim = run("simulate-skill", "--skill-id", skill_id, "--input-content", SIM_UTTERANCE, "--device-locale", "en-US")
    if not sim or "id" not in sim:
        print("[FAIL] simulate: could not start (ask configure? skill enabled in development?)")
        return False
    for _ in range(30):
        res = run("get-skill-simulation", "--skill-id", skill_id, "--simulation-id", sim["id"])
        if res and res.get("status") != "IN_PROGRESS":
            break
        time.sleep(3)
    ms = int((time.perf_counter() - t) * 1000)
    status = (res or {}).get("status")
    inv = (((res or {}).get("result") or {}).get("skillExecutionInfo") or {}).get("invocations") or [{}]
    intent = (((inv[0].get("invocationRequest") or {}).get("body") or {}).get("request") or {}).get("intent", {}).get("name")
    speech = json.dumps((((inv[0].get("invocationResponse") or {}).get("body") or {}).get("response") or {}).get("outputSpeech") or {})
    ok = status == "SUCCESSFUL" and intent == "HermesQueryIntent" and not any(e in speech for e in ERRORS)
    print(f"[{'ok' if ok else 'FAIL'}] alexa simulate-skill: {status}, intent={intent}, {ms} ms")
    return ok


def main(argv: list[str]) -> int:
    results = []
    if not secrets.exists("ha_alexa_token"):
        print("[FAIL] ha_alexa_token missing from DPAPI: run python -m deploy.bootstrap_ha")
        return 1
    bearer = {"Authorization": "Bearer " + secrets.get("ha_alexa_token")}
    results.append(check_turn("HA bridge (loopback)", LOCAL, bearer))
    access = {}
    if secrets.exists("cf_access_client_id") and secrets.exists("cf_access_client_secret"):
        access = {"CF-Access-Client-Id": secrets.get("cf_access_client_id"), "CF-Access-Client-Secret": secrets.get("cf_access_client_secret")}
    results.append(check_turn("public tunnel" + (" + Access" if access else ""), f"https://{state.get('public_hostname')}{PATH}", {**bearer, **access}))
    skill_id = state.load().get("skill_id")
    if "--skip-lambda" not in argv:
        results.append(check_lambda(skill_id) if skill_id else (print("[FAIL] lambda: no skill_id in state.json (run skill_deploy)") or False))
    if "--skip-simulate" not in argv:
        results.append(check_simulate(skill_id) if skill_id else (print("[FAIL] simulate: no skill_id in state.json") or False))
    print("ALL HOPS PASSED" if all(results) else "FAILED")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
