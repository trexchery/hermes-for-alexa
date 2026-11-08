"""Config flow, setup, sessions and the conversation entity (S4, S5, D2, D3 and the removed Echo screen)."""

import asyncio
from datetime import timedelta
from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.components import conversation
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import Context, HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest

from custom_components.hermes_voice.alexa import DATA_ALEXA, bridge_entry
from custom_components.hermes_voice.alexa_core import TurnResult
from custom_components.hermes_voice.const import DOMAIN
from custom_components.hermes_voice.session import SessionResolver

from .conftest import make_entry


async def start_user_flow(hass: HomeAssistant, url: str, chat):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"base_url": url, "api_key": "k" * 24, "display_name": "Voice"}
    )


@pytest.mark.parametrize(
    "url", ["http://hermes.example.com:8642/v1", "http://8.8.8.8:8642", "http://hermes.local:8642"]
)
async def test_s4_flow_refuses_plain_http_to_remote_hosts(hass, chat, url):
    result = await start_user_flow(hass, url, chat)
    assert result["type"] is FlowResultType.FORM and result["errors"] == {"base_url": "insecure_url"}


@pytest.mark.parametrize(
    "url",
    ["https://hermes.example.com/v1", "http://host.docker.internal:8642/v1", "http://192.168.1.5:8642", "http://127.0.0.1:8642"],
)
async def test_s4_flow_accepts_https_and_local_http(hass, chat, url):
    result = await start_user_flow(hass, url, chat)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["base_url"].endswith("/v1")


async def test_flow_still_rejects_garbage_urls(hass, chat):
    result = await start_user_flow(hass, "not a url", chat)
    assert result["errors"] == {"base_url": "invalid_url"}


async def test_s4_existing_insecure_entry_does_not_load(hass, chat):
    entry = make_entry(hass)
    hass.config_entries.async_update_entry(entry, data={**entry.data, "base_url": "http://hermes.example.com:8642/v1"})
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    chat.assert_not_called()


async def test_options_flow_has_alexa_bridge_and_no_screen_options(hass, entry):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    keys = {str(k) for k in result["data_schema"].schema}
    assert "alexa_bridge" in keys
    assert not keys & {"screen_activity", "activity_port"}
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"alexa_bridge": True, "session_mode": "per_conversation", "idle_reset_minutes": 30, "voice_cleanup": True, "fallback_message": "x", "timeout": 60})
    assert result["type"] is FlowResultType.CREATE_ENTRY and result["data"]["alexa_bridge"] is True


async def test_echo_screen_feature_is_gone(hass, hass_client_no_auth, entry):
    client = await hass_client_no_auth()
    for path in ("/hermes-screen", "/hermes_voice/screen", "/api/hermes_voice/screen"):
        assert (await client.get(path)).status in (401, 404)
    assert not hasattr(__import__("custom_components.hermes_voice.const", fromlist=["x"]), "CONF_SCREEN_ACTIVITY")


async def test_bridge_entry_prefers_the_flagged_one(hass, chat):
    a = make_entry(hass, stream=False)
    b = make_entry(hass, stream=False, alexa_bridge=True)
    b.mock_state(hass, ConfigEntryState.LOADED)
    a.mock_state(hass, ConfigEntryState.LOADED)
    assert bridge_entry(hass) is b
    hass.config_entries.async_update_entry(b, options={})
    assert bridge_entry(hass) is a


async def test_d2_d5_conversation_entity_namespaces_session_and_memory_scope(hass, chat):
    entry = make_entry(hass, stream=False, user_key="{user_id}/{device_id}")
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    for user in ("user-a", "user-b"):
        await conversation.async_converse(hass, "hi", "conv-1", Context(user_id=user), agent_id="conversation.hermes")
    first, second = (c.kwargs for c in chat.await_args_list)
    assert first["session_id"] != second["session_id"]  # same conversation id, different users
    assert first["session_id"].startswith("ha-u") and "user-a" not in first["session_id"]
    assert first["user_key"] == "user-a/conv-conv-1"  # no device: its own scope, never "nodevice"
    await conversation.async_converse(hass, "hi", "conv-2", Context(), agent_id="conversation.hermes")
    assert chat.await_args.kwargs["user_key"] == "conv-conv-2/conv-conv-2"  # no user either: never "anonymous"


async def test_d3_idle_device_windows_are_pruned(hass, freezer):
    resolver = SessionResolver(hass, "e1")
    with patch.object(resolver._store, "async_delay_save"):
        for dev in ("a", "b", "c"):
            await resolver.async_resolve(
                mode="per_device", conversation_id="c", device_id=dev, user_id="u", idle_reset_minutes=30
            )
        assert len(resolver.windows) == 3
        freezer.tick(timedelta(minutes=31))
        sid = await resolver.async_resolve(
            mode="per_device", conversation_id="c", device_id="a", user_id="u", idle_reset_minutes=30
        )
    assert len(resolver.windows) == 1  # b and c were idle too long; a started a new window
    assert sid.startswith("ha-dev-u") and "-u-" not in sid  # user hash, not the raw id


async def test_unload_keeps_turns_and_removal_cancels_them(hass, entry):
    state = hass.data[DATA_ALEXA]

    async def hang():
        await asyncio.sleep(60)
        return TurnResult("never", "hermes")

    turn = state.turns.queue("s").submit(hang, "fb")
    await asyncio.sleep(0)
    assert await hass.config_entries.async_unload(entry.entry_id)  # what an options save does
    await asyncio.sleep(0)
    assert not turn.done() and len(state.turns) == 1
    await hass.config_entries.async_remove(entry.entry_id)
    await asyncio.sleep(0)
    assert turn.cancelled() and len(state.turns) == 0
