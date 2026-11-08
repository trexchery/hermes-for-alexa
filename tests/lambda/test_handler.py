import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from hermes_alexa_skill import handler

SKILL = "amzn1.ask.skill.test"
ROOT = Path(__file__).resolve().parents[2]
TOKEN = "ha-token-SECRET"


class Fake:
    """Fake HA + Alexa API on one local server."""

    def __init__(self):
        self.calls = []
        self.status = 200
        self.body = {"status": "answered", "speech": "It is sunny.", "room": "Kitchen", "source": "hermes", "end_session": False}
        self.delay = 0.0
        self.raw = None  # plain-text body instead of JSON


@pytest.fixture
def fake(monkeypatch):
    f = Fake()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            f.calls.append((self.path, {k.lower(): v for k, v in self.headers.items()}, json.loads(raw)))
            if self.path == "/v1/directives":
                self.send_response(204)
                self.end_headers()
                return
            time.sleep(f.delay)
            data = f.raw.encode() if f.raw is not None else json.dumps(f.body).encode()
            self.send_response(f.status)
            self.send_header("Content-Type", "text/plain" if f.raw is not None else "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    f.url = f"http://127.0.0.1:{srv.server_port}"
    monkeypatch.setenv("SKILL_ID", SKILL)
    monkeypatch.setattr(handler, "_CONFIG", {
        "url": f.url + handler.TURN_PATH,
        "headers": {"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json", "CF-Access-Client-Id": "cid"},
    })
    monkeypatch.setattr(handler, "_TEST_ALLOW_ANY_ENDPOINT", True)
    yield f
    srv.shutdown()


def event(request, *, apl=False, app=SKILL, endpoint=None):
    system = {
        "application": {"applicationId": app},
        "device": {"deviceId": "amzn1.ask.device.D1", "supportedInterfaces": {"Alexa.Presentation.APL": {}} if apl else {}},
        "user": {"userId": "amzn1.ask.account.U1"},
        "person": {"personId": "amzn1.ask.person.P1"},
    }
    if endpoint:
        system.update(apiEndpoint=endpoint, apiAccessToken="api-token")
    return {"session": {"sessionId": "amzn1.echo-api.session.S1", "application": {"applicationId": app}},
            "context": {"System": system}, "request": dict(request, requestId="amzn1.echo-api.request.R1", locale="en-US")}


def intent(name, query=None):
    r = {"type": "IntentRequest", "intent": {"name": name}}
    if name == "HermesQueryIntent":
        r["intent"]["slots"] = {"query": {"name": "query", "value": query}}
    return r


def run(ev):
    return handler.lambda_handler(ev, None)["response"]


def ha_calls(fake):
    return [c for c in fake.calls if c[0] == handler.TURN_PATH]


def test_skill_id_rejected(fake):
    out = run(event(intent("HermesQueryIntent", "hi"), app="amzn1.ask.skill.other"))
    assert out == {} and not fake.calls


def test_missing_skill_id_env_rejects(fake, monkeypatch):
    monkeypatch.delenv("SKILL_ID")
    assert run(event(intent("HermesQueryIntent", "hi"))) == {} and not fake.calls


def test_launch(fake):
    fake.body = dict(fake.body, speech="")
    out = run(event({"type": "LaunchRequest"}))
    body = ha_calls(fake)[0][2]
    assert body["kind"] == "launch" and body["text"] == ""
    assert "Hermes here" in out["outputSpeech"]["ssml"] and out["shouldEndSession"] is False and "reprompt" in out


def test_query_answered_and_request_shape(fake):
    out = run(event(intent("HermesQueryIntent", " what is the weather ")))
    _, headers, body = ha_calls(fake)[0]
    assert body == {"alexa_device_id": "amzn1.ask.device.D1", "alexa_user_id": "amzn1.ask.account.U1",
                    "alexa_person_id": "amzn1.ask.person.P1", "alexa_session_id": "amzn1.echo-api.session.S1",
                    "text": "what is the weather", "kind": "utterance", "budget_ms": 5500,
                    "locale": "en-US", "has_screen": False}
    assert headers["authorization"] == "Bearer " + TOKEN and headers["cf-access-client-id"] == "cid"
    assert out["outputSpeech"]["ssml"] == "<speak>It is sunny.</speak>"
    assert out["shouldEndSession"] is False
    assert "Anything else" in out["reprompt"]["outputSpeech"]["ssml"]


def test_end_session_from_ha(fake):
    fake.body = dict(fake.body, end_session=True)
    out = run(event(intent("HermesQueryIntent", "bye")))
    assert out["shouldEndSession"] is True and "reprompt" not in out


def test_pending(fake):
    fake.body = {"status": "pending", "speech": "Still working on it.", "room": None, "source": "pending", "end_session": False}
    out = run(event(intent("HermesQueryIntent", "research x")))
    assert "Still working" in out["outputSpeech"]["ssml"]
    assert "what did you find" in out["reprompt"]["outputSpeech"]["ssml"] and out["shouldEndSession"] is False


def test_resume(fake):
    run(event(intent("ContinueIntent")))
    body = ha_calls(fake)[0][2]
    assert body["kind"] == "resume" and body["text"] == ""


@pytest.mark.parametrize("name", ["AMAZON.StopIntent", "AMAZON.CancelIntent", "AMAZON.NavigateHomeIntent"])
def test_stop_ends(fake, name):
    out = run(event(intent(name)))
    assert out["shouldEndSession"] is True and "Goodbye" in out["outputSpeech"]["ssml"]
    assert ha_calls(fake)[0][2]["kind"] == "end"


@pytest.mark.parametrize("name,text", [("AMAZON.YesIntent", "yes"), ("AMAZON.NoIntent", "no")])
def test_yes_no_reach_hermes(fake, name, text):
    out = run(event(intent(name)))
    body = ha_calls(fake)[0][2]
    assert body["kind"] == "utterance" and body["text"] == text and out["shouldEndSession"] is False


def test_session_ended_empty_response(fake):
    out = run(event({"type": "SessionEndedRequest", "reason": "USER_INITIATED"}))
    assert out == {} and ha_calls(fake)[0][2]["kind"] == "end"


@pytest.mark.parametrize("value", [None, "", "   "])
def test_empty_slot_reprompts_without_ha(fake, value):
    out = run(event(intent("HermesQueryIntent", value)))
    assert "reprompt" in out and out["shouldEndSession"] is False and not fake.calls


def test_fallback_and_help_and_unknown(fake):
    for name in ("AMAZON.FallbackIntent", "AMAZON.HelpIntent", "SomethingElse"):
        out = run(event(intent(name)))
        assert out["shouldEndSession"] is False and "reprompt" in out
    assert not fake.calls


def test_ha_timeout_is_safe_and_keeps_session(fake, monkeypatch):
    monkeypatch.setattr(handler, "BUDGET_MS", 1000)
    fake.delay = 2.6  # read timeout is budget/1000 + 1.0 = 2.0 s
    out = run(event(intent("HermesQueryIntent", "slow")))
    assert "couldn't reach Hermes" in out["outputSpeech"]["ssml"] and out["shouldEndSession"] is False


def test_ha_5xx_is_safe(fake):
    fake.status, fake.body = 503, {"error": {"code": "upstream_unavailable", "message": "x"}}
    out = run(event(intent("HermesQueryIntent", "hi")))
    assert "couldn't reach Hermes" in out["outputSpeech"]["ssml"] and out["shouldEndSession"] is False


def test_ha_401_error_shape(fake):
    fake.status, fake.body = 401, {"error": {"code": "unauthorized", "message": "bad token"}}
    out = run(event(intent("HermesQueryIntent", "hi")))
    assert "credentials" in out["outputSpeech"]["ssml"] and "bad token" not in out["outputSpeech"]["ssml"]


@pytest.mark.parametrize("status", [401, 403])
def test_ha_plain_text_auth_failure_is_credential_error(fake, status):
    fake.status, fake.raw = status, "401: Unauthorized"
    out = run(event(intent("HermesQueryIntent", "hi")))
    assert "credentials" in out["outputSpeech"]["ssml"]


def test_budget_follows_remaining_lambda_time(fake):
    class Ctx:
        def get_remaining_time_in_millis(self):
            return 7000

    handler.lambda_handler(event(intent("HermesQueryIntent", "hi")), Ctx())
    assert ha_calls(fake)[0][2]["budget_ms"] == 2500


def test_config_sends_fixed_user_agent(monkeypatch):
    monkeypatch.setattr(handler, "_fetch_ssm", lambda prefix: {"ha_url": "https://ha.example", "ha_token": "t"})
    assert handler._load_config()["headers"]["User-Agent"] == handler.USER_AGENT


def test_malformed_ha_reply(fake):
    fake.body = {"status": "weird"}
    out = run(event(intent("HermesQueryIntent", "hi")))
    assert "couldn't reach Hermes" in out["outputSpeech"]["ssml"]


def test_apl_only_when_supported(fake):
    plain = run(event(intent("HermesQueryIntent", "q")))
    assert "directives" not in plain
    out = run(event(intent("HermesQueryIntent", "q?"), apl=True))
    d = out["directives"][0]
    assert d["type"] == "Alexa.Presentation.APL.RenderDocument" and d["token"] == "hermes"
    assert d["document"]["type"] == "APL"
    assert d["datasources"]["data"] == {"room": "Kitchen", "question": "q?", "answer": "It is sunny."}
    assert ha_calls(fake)[-1][2]["has_screen"] is True


def test_xml_escaping_and_cyrillic(fake):
    fake.body = dict(fake.body, speech="Свет включён & <b>ok</b> \x00")
    ssml = run(event(intent("HermesQueryIntent", "свет")))["outputSpeech"]["ssml"]
    assert ssml == "<speak>Свет включён &amp; &lt;b&gt;ok&lt;/b&gt; </speak>"
    assert ha_calls(fake)[0][2]["text"] == "свет"


def test_speech_truncated_to_alexa_limit():
    assert len(handler._ssml("&" * 20000)) <= handler.SPEAK_LIMIT


def test_no_secrets_or_utterances_in_logs(fake, caplog):
    caplog.set_level(logging.DEBUG, logger="hermes_alexa_skill")
    fake.body = dict(fake.body, speech="SECRET-ANSWER")
    run(event(intent("HermesQueryIntent", "SECRET-UTTERANCE"), endpoint=fake.url))
    fake.status = 500
    run(event(intent("HermesQueryIntent", "SECRET-UTTERANCE")))
    assert caplog.records
    for needle in ("SECRET-UTTERANCE", "SECRET-ANSWER", TOKEN, "api-token"):
        assert needle not in caplog.text


def directives(fake):
    return [c for c in fake.calls if c[0] == "/v1/directives"]


def wait_for(cond, timeout=2.0):
    end = time.time() + timeout
    while time.time() < end and not cond():
        time.sleep(0.02)


def test_progressive_response_fires_when_slow(fake, monkeypatch):
    monkeypatch.setattr(handler, "PROGRESSIVE_DELAY", 0.05)
    fake.delay = 0.5
    run(event(intent("HermesQueryIntent", "slow"), endpoint=fake.url))
    wait_for(lambda: directives(fake))
    _, headers, body = directives(fake)[0]
    assert headers["authorization"] == "Bearer api-token"
    assert body["header"]["requestId"] == "amzn1.echo-api.request.R1"
    assert body["directive"] == {"type": "VoicePlayer.Speak", "speech": "<speak>One moment.</speak>"}


def test_progressive_response_cancelled_when_fast(fake, monkeypatch):
    monkeypatch.setattr(handler, "PROGRESSIVE_DELAY", 0.3)
    run(event(intent("HermesQueryIntent", "fast"), endpoint=fake.url))
    time.sleep(0.6)
    assert not directives(fake)


def test_progressive_endpoint_must_be_amazon(fake, monkeypatch):
    monkeypatch.setattr(handler, "_TEST_ALLOW_ANY_ENDPOINT", False)
    monkeypatch.setattr(handler, "PROGRESSIVE_DELAY", 0.05)
    fake.delay = 0.3
    run(event(intent("HermesQueryIntent", "x"), endpoint=fake.url))
    time.sleep(0.5)
    assert not directives(fake)


def test_config_requires_https(monkeypatch):
    monkeypatch.setattr(handler, "_fetch_ssm", lambda prefix: {"ha_url": "http://ha.example", "ha_token": "t"})
    with pytest.raises(ValueError):
        handler._load_config()
    monkeypatch.setattr(handler, "_fetch_ssm", lambda prefix: {"ha_url": "https://ha.example/", "ha_token": "t", "cf_access_client_id": "i", "cf_access_client_secret": "s"})
    cfg = handler._load_config()
    assert cfg["url"] == "https://ha.example" + handler.TURN_PATH
    assert cfg["headers"]["CF-Access-Client-Secret"] == "s"


def test_interaction_model_valid():
    m = json.loads((ROOT / "skill-package/interactionModels/custom/en-US.json").read_text(encoding="utf-8"))["interactionModel"]["languageModel"]
    assert m["invocationName"] == "hermes"
    names = [i["name"] for i in m["intents"]]
    assert len(names) == len(set(names))
    for required in ("AMAZON.StopIntent", "AMAZON.CancelIntent", "AMAZON.HelpIntent", "AMAZON.FallbackIntent",
                     "AMAZON.NavigateHomeIntent", "HermesQueryIntent", "ContinueIntent"):
        assert required in names
    seen = set()
    for i in m["intents"]:
        slots = {s["name"]: s["type"] for s in i.get("slots", [])}
        for s in i["samples"]:
            assert s.lower() not in seen, s
            seen.add(s.lower())
            for ref in __import__("re").findall(r"\{(\w+)\}", s):
                assert ref in slots
                if slots[ref] == "AMAZON.SearchQuery":
                    assert s.replace("{" + ref + "}", "").strip(), "SearchQuery needs a carrier phrase"
    q = next(i for i in m["intents"] if i["name"] == "HermesQueryIntent")
    assert q["slots"][0]["type"] == "AMAZON.SearchQuery"


def test_skill_manifest():
    m = json.loads((ROOT / "skill-package/skill.json").read_text(encoding="utf-8"))["manifest"]
    assert m["apis"]["custom"]["endpoint"]["uri"] == "__LAMBDA_ARN__"
    assert {"name": "alexa::person_id:read"} in m["permissions"]
    assert m["apis"]["custom"]["interfaces"] == [{"type": "ALEXA_PRESENTATION_APL"}]
    assert "en-US" in m["publishingInformation"]["locales"]
