"""Hermes Voice: use a Hermes Agent profile as a Home Assistant conversation agent."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryError, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .alexa import DATA_ALEXA, AlexaTurnView
from .alexa_core import AlexaBridgeState
from .client import HermesAuthError, HermesClient, HermesError
from .const import CONF_API_KEY, CONF_BASE_URL, CONF_MODEL, DOMAIN
from .rules import base_url_allowed
from .session import SessionResolver

PLATFORMS = (Platform.CONVERSATION,)
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


@dataclass(slots=True)
class HermesRuntimeData:
    """Per-entry runtime objects."""

    client: HermesClient
    model: str
    sessions: SessionResolver


type HermesConfigEntry = ConfigEntry[HermesRuntimeData]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up what all entries share: the Alexa bridge view (alexa.py)."""
    hass.data[DATA_ALEXA] = AlexaBridgeState(
        spawn=lambda coro: hass.async_create_background_task(coro, "hermes_voice_alexa_turn")
    )
    hass.http.register_view(AlexaTurnView())
    return True


async def async_setup_entry(hass: HomeAssistant, entry: HermesConfigEntry) -> bool:
    """Set up one Hermes profile."""
    if not base_url_allowed(entry.data[CONF_BASE_URL]):  # S4
        raise ConfigEntryError("base_url must be https, or http to loopback, host.docker.internal or a private IP")
    client = HermesClient(async_get_clientsession(hass), entry.data[CONF_BASE_URL], entry.data[CONF_API_KEY])
    try:
        models = await client.async_list_models()
    except HermesAuthError as err:
        raise ConfigEntryAuthFailed("Hermes rejected the API key") from err
    except HermesError as err:
        raise ConfigEntryNotReady(f"Hermes gateway not reachable: {err}") from err

    model = entry.data.get(CONF_MODEL) or (models[0] if models else "hermes-agent")
    sessions = SessionResolver(hass, entry.entry_id)
    await sessions.async_load()
    entry.runtime_data = HermesRuntimeData(client=client, model=model, sessions=sessions)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: HermesConfigEntry) -> bool:
    """Unload an entry. Turns in flight are kept: an options save reloads the entry and must not drop answers."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: HermesConfigEntry) -> None:
    """The entry is deleted for good: no Hermes is left to run the turns still in flight."""
    others = [
        e
        for e in hass.config_entries.async_entries(DOMAIN)
        if e.entry_id != entry.entry_id and e.state is ConfigEntryState.LOADED
    ]
    if not others:
        hass.data[DATA_ALEXA].cancel_all()
