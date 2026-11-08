"""Alexa custom-skill Lambda for Hermes (stdlib only; boto3 is imported lazily for SSM).

Entry point: hermes_alexa_skill.handler.lambda_handler. Interfaces: docs/ARCHITECTURE.md.

Interaction model choice (skill-package/interactionModels/custom/en-US.json): AMAZON.SearchQuery
is the only slot type that captures free speech, and Alexa rejects it in a bare "{query}" sample,
so HermesQueryIntent uses filler carrier phrases ("ask {query}", "tell me {query}", "please {query}").
Carrier words are not part of the slot value, so keep them filler. The bare AMAZON.Person
"{query}" trick (used upstream) is the fallback if coverage proves poor on real devices.

Deploy-time placeholders in skill.json: __LAMBDA_ARN__ (endpoint) and __PRIVACY_POLICY_URL__
(required because of the alexa::person_id:read permission).
"""

import html
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger("hermes_alexa_skill")
_LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}
logger.setLevel(_LEVELS.get(os.environ.get("LOG_LEVEL", "").strip().upper(), logging.INFO))

TURN_PATH = "/api/hermes_voice/alexa/v1/turn"
BUDGET_MS = 5500  # contract: first-call budget; HA read timeout = budget/1000 + 1.0
PROGRESSIVE_DELAY = 2.0
SPEAK_LIMIT = 8000  # Alexa outputSpeech limit
MAX_REPLY_BYTES = 256 * 1024
_ID = re.compile(r"^[A-Za-z0-9._:-]{1,512}$")
_LOCALE = re.compile(r"^[a-z]{2}-[A-Z]{2}$")
_XML_BAD = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")
_AMAZON_HOST = re.compile(r"^api(\.[a-z]+)?\.amazonalexa\.com$")
_TEST_ALLOW_ANY_ENDPOINT = False  # tests only: lets the progressive POST target a local fake

REPROMPT = "Anything else?"
ASK = "What would you like to ask Hermes?"
HELP = "Ask me anything, or tell me what to do around the house. Say stop when you are done."
GREETING = "Hermes here. What can I do for you?"
GOODBYE = "Goodbye."
SAFE_ERROR = "I couldn't reach Hermes just now."
PENDING_REPROMPT = "Say 'what did you find' in a few seconds."
ERROR_SPEECH = {
    "rate_limited": "Hermes is busy right now. Try again in a moment.",
    "unauthorized": "Hermes did not accept my credentials. Check the skill setup.",
}
_END_INTENTS = {"AMAZON.StopIntent", "AMAZON.CancelIntent", "AMAZON.NavigateHomeIntent"}
_YES_NO = {"AMAZON.YesIntent": "yes", "AMAZON.NoIntent": "no"}  # answers to a Hermes question: sent as utterances
# HTTP status -> contract error code, used before (and unless overridden by) a JSON error body.
_STATUS_CODE = {401: "unauthorized", 403: "unauthorized", 302: "unauthorized", 413: "invalid_request", 429: "rate_limited"}
_CODES = {"unauthorized", "rate_limited", "invalid_request", "upstream_unavailable", "internal"}
USER_AGENT = "hermes-alexa-lambda/1"  # Cloudflare Browser Integrity Check blocks the default Python-urllib UA
SPARE_MS = 4500  # of the Lambda time left, kept for cold start leftovers, the reply and Alexa's own overhead

_CONFIG = None  # the only mutable module global: SSM config, fetched once per cold start


class _HaError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


# ---------------------------------------------------------------- config

def _fetch_ssm(prefix):
    import boto3  # in the Lambda runtime; lazy so tests need no boto3

    out = {}
    from botocore.config import Config

    cfg = Config(connect_timeout=1, read_timeout=2, retries={"max_attempts": 1})
    pages = boto3.client("ssm", config=cfg).get_paginator("get_parameters_by_path").paginate(Path=prefix, WithDecryption=True)
    for page in pages:
        for p in page["Parameters"]:
            out[p["Name"][len(prefix):]] = p["Value"]
    return out


def _load_config():
    prefix = os.environ.get("SSM_PREFIX", "/hermes-alexa/")
    if not prefix.endswith("/"):
        prefix += "/"
    params = _fetch_ssm(prefix)
    url = params.get("ha_url", "").strip().rstrip("/")
    u = urllib.parse.urlparse(url)
    if u.scheme != "https" or not u.hostname or u.username or u.password or u.query or u.fragment:
        raise ValueError("ha_url must be a plain https URL")
    token = params.get("ha_token", "").strip()
    if not token:
        raise ValueError("ha_token missing")
    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json", "User-Agent": USER_AGENT}
    cid = params.get("cf_access_client_id", "").strip()
    secret = params.get("cf_access_client_secret", "").strip()
    if cid and secret:
        headers["CF-Access-Client-Id"] = cid
        headers["CF-Access-Client-Secret"] = secret
    return {"url": url + TURN_PATH, "headers": headers}


def _config():
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = _load_config()
    return _CONFIG


# ---------------------------------------------------------------- speech / response

def _ssml(text):
    """Escape plain text into SSML. Keeps all Unicode; drops only XML-illegal control chars."""
    out, n = [], 0
    for ch in _XML_BAD.sub("", text):
        e = html.escape(ch, quote=False)
        n += len(e)
        if n > SPEAK_LIMIT - len("<speak></speak>"):
            break
        out.append(e)
    return "<speak>" + "".join(out) + "</speak>"


def _speech(text):
    return {"type": "SSML", "ssml": _ssml(text)}


def _resp(speech=None, reprompt=None, end=False, directives=None):
    r = {"shouldEndSession": end}
    if speech:
        r["outputSpeech"] = _speech(speech)
    if reprompt and not end:
        r["reprompt"] = {"outputSpeech": _speech(reprompt)}
    if directives:
        r["directives"] = directives
    return {"version": "1.0", "response": r}


@lru_cache(maxsize=1)
def _apl_doc():
    return json.loads(Path(__file__).with_name("apl_answer.json").read_text(encoding="utf-8"))


def _apl(room, question, answer):
    return {
        "type": "Alexa.Presentation.APL.RenderDocument",
        "token": "hermes",
        "document": _apl_doc(),
        "datasources": {"data": {"room": room or "", "question": question[:500], "answer": answer[:6000]}},
    }


# ---------------------------------------------------------------- HTTP

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # never forward the bearer token to a redirect target


_opener = urllib.request.build_opener(_NoRedirect)


def _ask_hermes(body, budget_ms):
    """POST one turn to HA. Returns the validated success dict or raises _HaError(code)."""
    try:
        cfg = _config()
    except Exception as exc:
        logger.error("config load failed: %s", type(exc).__name__)
        raise _HaError("internal")
    req = urllib.request.Request(cfg["url"], data=json.dumps(body).encode(), headers=cfg["headers"], method="POST")
    # ponytail: urllib has one socket timeout (not separate 2 s connect); fits the 8 s Alexa window.
    try:
        with _opener.open(req, timeout=budget_ms / 1000 + 1.0) as r:
            data = json.loads(r.read(MAX_REPLY_BYTES))
    except urllib.error.HTTPError as e:
        code = _STATUS_CODE.get(e.code, "upstream_unavailable" if e.code >= 500 else "internal")
        try:
            c = json.loads(e.read(MAX_REPLY_BYTES))["error"]["code"]
            if c in _CODES:
                code = c
        except Exception:
            pass
        logger.warning("HA answered HTTP %s -> %s", e.code, code)
        raise _HaError(code)
    except (OSError, ValueError):  # URLError, timeout, bad JSON
        raise _HaError("upstream_unavailable")
    if not isinstance(data, dict) or data.get("status") not in ("answered", "pending") or not isinstance(data.get("speech"), str):
        raise _HaError("internal")
    room = data.get("room")
    return {
        "status": data["status"],
        "speech": data["speech"],
        "room": room if isinstance(room, str) else None,
        "end": data.get("end_session") is True,
    }


def _send_progressive(endpoint, token, request_id):
    try:
        body = {"header": {"requestId": request_id},
                "directive": {"type": "VoicePlayer.Speak", "speech": "<speak>One moment.</speak>"}}
        req = urllib.request.Request(
            endpoint.rstrip("/") + "/v1/directives", data=json.dumps(body).encode(), method="POST",
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        with _opener.open(req, timeout=2.0):
            pass
    except Exception as exc:  # never raise from the timer thread
        logger.debug("progressive response failed: %s", type(exc).__name__)


def _start_progressive(system, request):
    endpoint, token, rid = system.get("apiEndpoint"), system.get("apiAccessToken"), request.get("requestId")
    if not (isinstance(endpoint, str) and isinstance(token, str) and isinstance(rid, str) and token and rid):
        return None
    u = urllib.parse.urlparse(endpoint)
    if not _TEST_ALLOW_ANY_ENDPOINT and not (u.scheme == "https" and _AMAZON_HOST.match(u.hostname or "")):
        return None
    t = threading.Timer(PROGRESSIVE_DELAY, _send_progressive, (endpoint, token, rid))
    t.daemon = True
    t.start()
    return t


# ---------------------------------------------------------------- dispatch

def _ids(system, session):
    def clean(v):
        return v if isinstance(v, str) and _ID.match(v) else None

    device = system.get("device") or {}
    person = system.get("person") or {}
    user = system.get("user") or {}
    return {
        "alexa_device_id": clean(device.get("deviceId")),
        "alexa_user_id": clean(user.get("userId")),
        "alexa_person_id": clean(person.get("personId")),
        "alexa_session_id": clean(session.get("sessionId")),
    }


def _turn(kind, text, system, session, request, budget_ms=None):
    budget_ms = budget_ms or BUDGET_MS
    ids = _ids(system, session)
    if not (ids["alexa_device_id"] and ids["alexa_user_id"] and ids["alexa_session_id"]):
        raise _HaError("invalid_request")
    locale = request.get("locale")
    body = dict(ids, text=text[:1000], kind=kind, budget_ms=budget_ms,
                locale=locale if isinstance(locale, str) and _LOCALE.match(locale) else "en-US",
                has_screen="Alexa.Presentation.APL" in ((system.get("device") or {}).get("supportedInterfaces") or {}))
    return _ask_hermes(body, budget_ms)


def _answer_response(result, text, has_screen):
    speech = result["speech"] or GREETING
    reprompt = PENDING_REPROMPT if result["status"] == "pending" else REPROMPT
    directives = None
    if has_screen and result["status"] == "answered":
        directives = [_apl(result["room"], text, speech)]
    return _resp(speech, reprompt, end=result["end"], directives=directives)


def _handle(event, remaining_ms=None):
    if not isinstance(event, dict) or not isinstance(event.get("request"), dict):
        return "invalid", _resp(), "rejected"
    request = event["request"]
    rtype = request.get("type")
    session = event.get("session") if isinstance(event.get("session"), dict) else {}
    system = (event.get("context") or {}).get("System")
    system = system if isinstance(system, dict) else {}

    app_ids = {(d.get("application") or {}).get("applicationId") for d in (session, system) if isinstance(d, dict)}
    app_ids.discard(None)
    skill_id = os.environ.get("SKILL_ID", "")
    if not skill_id or not app_ids or app_ids != {skill_id}:
        logger.warning("rejected request: application id mismatch")
        return str(rtype), {"version": "1.0", "response": {}}, "rejected"

    has_screen = "Alexa.Presentation.APL" in ((system.get("device") or {}).get("supportedInterfaces") or {})
    kind, text = "utterance", ""
    if rtype == "SessionEndedRequest":
        try:
            _turn("end", "end", system, session, request, budget_ms=1000)
        except _HaError:
            pass
        return rtype, {"version": "1.0", "response": {}}, "ok"
    if rtype == "LaunchRequest":
        kind = "launch"
    elif rtype == "IntentRequest":
        intent = request.get("intent") if isinstance(request.get("intent"), dict) else {}
        name = intent.get("name")
        if name == "HermesQueryIntent":
            value = ((intent.get("slots") or {}).get("query") or {}).get("value")
            text = value.strip() if isinstance(value, str) else ""
            if not text:
                return f"{rtype}:{name}", _resp(ASK, ASK), "empty"
        elif name == "ContinueIntent":
            kind = "resume"
        elif name in _YES_NO:
            text = _YES_NO[name]
        elif name in _END_INTENTS:
            try:
                _turn("end", "end", system, session, request, budget_ms=1000)
            except _HaError:
                pass
            return f"{rtype}:{name}", _resp(GOODBYE, end=True), "ok"
        elif name == "AMAZON.HelpIntent":
            return f"{rtype}:{name}", _resp(HELP, ASK), "ok"
        else:  # FallbackIntent and anything unknown
            return f"{rtype}:{name}", _resp("Sorry, I didn't get that. " + ASK, ASK), "fallback"
    else:
        return str(rtype), _resp("Sorry, I can't do that. " + ASK, ASK), "unsupported"

    timer = _start_progressive(system, request)
    try:
        budget = BUDGET_MS if remaining_ms is None else max(1000, min(BUDGET_MS, remaining_ms - SPARE_MS))
        result = _turn(kind, text, system, session, request, budget_ms=budget)
    except _HaError as e:
        return str(rtype), _resp(ERROR_SPEECH.get(e.code, SAFE_ERROR), REPROMPT), "error:" + e.code
    finally:
        if timer:
            timer.cancel()
    return str(rtype), _answer_response(result, text, has_screen), result["status"]


def lambda_handler(event, context):
    t0 = time.monotonic()
    try:
        left = getattr(context, "get_remaining_time_in_millis", None)
        rtype, out, status = _handle(event, int(left()) if left else None)
    except Exception as exc:  # last resort: speak, never crash the skill
        rtype, status = "unknown", "exception:" + type(exc).__name__
        out = _resp(SAFE_ERROR, REPROMPT)
    logger.info("request=%s status=%s latency_ms=%d", rtype, status, (time.monotonic() - t0) * 1000)
    return out
