"""The Alexa bridge view, end to end inside Home Assistant (registries, default agent, Hermes client mocked)."""

import asyncio
from unittest.mock import AsyncMock, patch

from homeassistant.components import conversation
from homeassistant.helpers import area_registry as ar, device_registry as dr, intent
import pytest

from custom_components.hermes_voice import alexa
from custom_components.hermes_voice.alexa_core import device_identifier
from custom_components.hermes_voice.client import HermesError, HermesReply

URL = "/api/hermes_voice/alexa/v1/turn"
DEVICE = "amzn1.ask.device.TESTDEVICE"
BODY = {
    "text": "what is the capital of France",
    "alexa_device_id": DEVICE,
    "alexa_user_id": "amzn1.ask.account.U1",
    "alexa_session_id": "amzn1.echo-api.session.S1",
}


def result(speech, code=None):
    response = intent.IntentResponse(language="en")
    if code:
        response.async_set_error(code, speech)
    else:
        response.async_set_speech(speech)
    return conversation.ConversationResult(response=response)


@pytest.fixture
def local():
    """The HA default agent: by default it understands nothing, so Hermes answers."""
    miss = result("Sorry, I did not understand", intent.IntentResponseErrorCode.NO_INTENT_MATCH)
    with patch.object(alexa.conversation, "async_converse", AsyncMock(return_value=miss)) as mock:
        yield mock


@pytest.fixture
async def client(hass, hass_client, entry, local):
    return await hass_client()


async def post(client, body=None, **changes):
    return await client.post(URL, json={**BODY, **(body or {}), **changes})


def assert_error(data, code):
    assert set(data) == {"error"} and data["error"]["code"] == code and isinstance(data["error"]["message"], str)


# --- errors ---------------------------------------------------------------------------------------


async def test_no_token_is_refused(hass, hass_client_no_auth, entry, local):
    resp = await (await hass_client_no_auth()).post(URL, json=BODY)
    assert resp.status == 401


async def test_input_validation_uses_the_error_shape(client):
    cases = [
        ({"unknown": 1}, 400),
        ({"text": ""}, 400),
        ({"alexa_device_id": "bad id"}, 400),
        ({"budget_ms": 100}, 400),
        ({"kind": "shout"}, 400),
    ]
    for changes, status in cases:
        resp = await post(client, **changes)
        assert resp.status == status, changes
        assert_error(await resp.json(), "invalid_request")


async def test_non_json_and_wrong_type_and_oversize(client):
    resp = await client.post(URL, data="not json", headers={"Content-Type": "application/json"})
    assert resp.status == 400
    assert_error(await resp.json(), "invalid_request")
    resp = await client.post(URL, data="{}", headers={"Content-Type": "text/plain"})
    assert resp.status == 400
    assert_error(await resp.json(), "invalid_request")
    resp = await client.post(URL, json=[1, 2])
    assert resp.status == 400
    big = await post(client, text="x" * 9000)
    assert big.status == 413
    assert_error(await big.json(), "invalid_request")

    async def chunks():  # no Content-Length: the view must still stop reading at 8 KiB
        for _ in range(10):
            yield b" " * 1000

    chunked = await client.post(URL, data=chunks(), headers={"Content-Type": "application/json"})
    assert chunked.status == 413


async def test_rate_limit_per_user(client):
    codes = [(await post(client, kind="end", text="")).status for _ in range(31)]
    assert codes[:30] == [200] * 30 and codes[30] == 429
    assert_error(await (await post(client, kind="end", text="")).json(), "rate_limited")


async def test_errors_never_leak_exception_text(client):
    with patch.object(alexa, "alexa_device", side_effect=RuntimeError("secret-token-123")):
        resp = await post(client)
    assert resp.status == 500
    body = await resp.json()
    assert_error(body, "internal")
    assert "secret" not in str(body)


async def test_no_loaded_entry_is_503(hass, hass_client, entry, local):
    client = await hass_client()
    assert await hass.config_entries.async_unload(entry.entry_id)
    resp = await post(client)
    assert resp.status == 503
    assert_error(await resp.json(), "upstream_unavailable")


# --- registries and room --------------------------------------------------------------------------


async def test_device_is_created_once_and_room_command_creates_area(hass, client, entry):
    resp = await post(client, text="this is the kitchen")
    data = await resp.json()
    assert resp.status == 200
    assert data["source"] == "room" and data["room"] == "Kitchen" and data["status"] == "answered"

    devices = dr.async_get(hass)
    device = devices.async_get_device_by_identifier(device_identifier(DEVICE), entry.entry_id)
    areas = ar.async_get(hass)
    assert device is not None and areas.async_get_area(device.area_id).name == "Kitchen"
    assert DEVICE not in str(device.identifiers) and DEVICE not in (device.name or "")  # only the hash

    data = await (await post(client, text="this device is in the living room")).json()
    assert data["room"] == "Living Room"
    assert len(dr.async_entries_for_config_entry(devices, entry.entry_id)) == 2  # the Hermes service + one Alexa
    assert areas.async_get_area_by_name("Living Room") is not None

    # an existing area is reused, not duplicated
    await post(client, text="you are in the kitchen")
    assert len([a for a in areas.async_list_areas() if a.name == "Kitchen"]) == 1


async def test_device_cap(hass, client, entry):
    with patch.object(alexa, "MAX_ALEXA_DEVICES", 2):
        for i in range(2):
            assert (await post(client, alexa_device_id=f"dev{i}", kind="end", text="")).status == 200
        resp = await post(client, alexa_device_id="dev-extra", kind="end", text="")
    assert resp.status == 429
    assert_error(await resp.json(), "rate_limited")


# --- the three answerers --------------------------------------------------------------------------


async def test_local_agent_answer_wins_and_gets_the_mapped_device(hass, client, entry, local, chat):
    local.return_value = result("Turned on the light")
    data = await (await post(client, text="turn on the light")).json()
    assert data == {"status": "answered", "speech": "Turned on the light", "room": None, "source": "local", "end_session": False}
    chat.assert_not_called()
    kwargs = local.await_args.kwargs
    device = dr.async_get(hass).async_get_device_by_identifier(device_identifier(DEVICE), entry.entry_id)
    assert kwargs["device_id"] == device.id and kwargs["agent_id"] == "conversation.home_assistant"
    assert local.await_args.args[2] is None  # no HA conversation id: Hermes keeps the thread


async def test_other_local_errors_are_spoken_but_misses_go_to_hermes(client, local, chat):
    local.return_value = result("Sorry, there was an error", intent.IntentResponseErrorCode.FAILED_TO_HANDLE)
    data = await (await post(client, text="do a thing")).json()
    assert data["source"] == "local" and data["speech"] == "Sorry, there was an error"
    chat.assert_not_called()

    for code in (intent.IntentResponseErrorCode.NO_INTENT_MATCH, intent.IntentResponseErrorCode.NO_VALID_TARGETS):
        local.return_value = result("miss", code)
        data = await (await post(client, text="turn on the light")).json()
        assert data["source"] == "hermes"
    local.side_effect = RuntimeError("agent exploded")  # a crashing local agent never blocks Hermes
    assert (await post(client, text="hello")).status == 200


async def test_hermes_gets_session_headers_system_message_and_clean_speech(client, chat):
    await post(client, text="this is the kitchen")
    chat.return_value = HermesReply("It is **Paris**. See [this](http://x.y).", None)
    data = await (await post(client, alexa_person_id="amzn1.ask.person.P1")).json()
    assert data["status"] == "answered" and data["source"] == "hermes" and data["room"] == "Kitchen"
    assert data["speech"] == "It is Paris. See this."
    kw = chat.await_args.kwargs
    assert kw["session_id"].startswith("alexa-person:") and "-kitchen-" in kw["session_id"]
    assert kw["user_key"] == "household"
    assert kw["user_text"] == BODY["text"]
    assert "The speaker is in the Kitchen." in kw["system_prompt"] and "Current local date and time" in kw["system_prompt"]
    assert "Speaker: a voice-recognised household member." in kw["system_prompt"]
    await post(client)  # no person id: household speaker, same room, different Hermes thread
    assert chat.await_args.kwargs["session_id"].startswith("alexa-household-kitchen-")


async def test_hermes_failure_speaks_the_fallback(client, chat):
    chat.side_effect = HermesError("boom: http://secret-host")
    data = await (await post(client)).json()
    assert data["status"] == "answered" and "can't reach my brain" in data["speech"]
    assert "secret-host" not in data["speech"]


async def test_goodbye_ends_the_session_without_asking_anyone(client, local, chat):
    data = await (await post(client, text="that's all, thanks")).json()
    assert data["end_session"] is True and data["source"] == "system"
    chat.assert_not_called()
    local.assert_not_called()


async def test_end_launch_resume_with_nothing_pending(client, chat):
    assert (await (await post(client, kind="end", text="")).json())["speech"] == ""
    data = await (await post(client, kind="resume", text="")).json()
    assert data["status"] == "answered" and data["source"] == "system"
    data = await (await post(client, kind="launch", text="")).json()
    assert data["status"] == "answered" and data["end_session"] is False
    chat.assert_not_called()


# --- pending --------------------------------------------------------------------------------------


@pytest.fixture
def slow_hermes(chat):
    release = asyncio.Event()

    async def slow(**kwargs):
        await release.wait()
        return HermesReply(f"Answer to {kwargs['user_text']}.", None)

    chat.side_effect = slow
    return release


async def test_slow_hermes_answers_pending_then_resume_delivers(client, slow_hermes, chat):
    data = await (await post(client, text="first question", budget_ms=1000)).json()
    assert data["status"] == "pending" and data["source"] == "pending" and data["speech"]
    # a second question while the first is running: queued behind it, same conversation
    data = await (await post(client, text="second question", budget_ms=1000)).json()
    assert data["status"] == "pending"
    assert chat.await_count == 1  # strictly one at a time
    slow_hermes.set()
    data = await (await post(client, kind="resume", text="", budget_ms=3000)).json()
    assert data["status"] == "answered" and data["speech"] == "Answer to first question. Answer to second question."
    again = await (await post(client, kind="resume", text="")).json()
    assert again["source"] == "system"  # delivered once


async def test_finished_answer_is_prepended_to_the_next_turn(client, slow_hermes, local):
    await post(client, text="slow one", budget_ms=1000)
    slow_hermes.set()
    await asyncio.sleep(0.1)
    local.return_value = result("Turned on the light")
    data = await (await post(client, text="turn on the light")).json()
    assert data["speech"] == "Answer to slow one. Turned on the light" and data["source"] == "local"


async def test_end_keeps_undelivered_answers(client, slow_hermes):
    await post(client, text="slow one", budget_ms=1000)
    assert (await post(client, kind="end", text="")).status == 200
    slow_hermes.set()
    data = await (await post(client, kind="resume", text="", budget_ms=3000)).json()
    assert data["speech"] == "Answer to slow one."


async def test_pending_answer_survives_a_room_change(client, slow_hermes):
    await post(client, text="slow one", budget_ms=1000)  # no room yet
    assert (await (await post(client, text="this is the kitchen")).json())["source"] == "room"
    slow_hermes.set()
    data = await (await post(client, kind="resume", text="", budget_ms=3000)).json()
    assert data["speech"] == "Answer to slow one." and data["room"] == "Kitchen"


async def test_voice_room_commands_stop_creating_areas_at_the_cap(hass, client):
    with patch.object(alexa, "MAX_VOICE_AREAS", 1):
        assert (await (await post(client, text="this is the kitchen")).json())["room"] == "Kitchen"
        data = await (await post(client, text="this device is in the garage")).json()
        assert "do not know that room" in data["speech"]
        assert ar.async_get(hass).async_get_area_by_name("Garage") is None
        assert (await (await post(client, text="this is the kitchen")).json())["room"] == "Kitchen"  # existing: fine


async def test_local_intent_with_empty_speech_is_not_repeated_to_hermes(client, local, chat):
    local.return_value = result("")
    data = await (await post(client, text="turn on the light")).json()
    assert data["source"] == "local" and data["speech"] == "Done."
    chat.assert_not_called()
