"""Config and options flow for Hermes Voice."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from yarl import URL

try:  # Home Assistant 2026.11 moved from voluptuous to probatio (same Schema/Required/Optional API)
    import probatio as vol
except ImportError:  # pragma: no cover
    import voluptuous as vol  # type: ignore[no-redef]

from .client import HermesAuthError, HermesClient, HermesError, normalize_base_url
from .const import (
    CONF_ALEXA_BRIDGE,
    CONF_API_KEY,
    CONF_AVOID_NAME,
    CONF_BASE_URL,
    CONF_DISPLAY_NAME,
    CONF_FALLBACK_MESSAGE,
    CONF_IDLE_RESET_MINUTES,
    CONF_MODEL,
    CONF_SEND_TIME,
    CONF_SESSION_MODE,
    CONF_STREAM,
    CONF_TIMEOUT,
    CONF_USER_KEY,
    CONF_VOICE_CLEANUP,
    CONF_VOICE_INSTRUCTION,
    CONF_WORKING_PHRASE,
    DEFAULT_DISPLAY_NAME,
    DEFAULT_OPTIONS,
    DOMAIN,
    LOGGER,
    SESSION_MODES,
)
from .rules import base_url_allowed


async def _validate(hass: Any, base_url: str, api_key: str) -> tuple[str, list[str]]:
    """Return (normalized url, model ids); raise HermesError on failure."""
    client = HermesClient(async_get_clientsession(hass), base_url, api_key)
    models = await client.async_list_models()
    return client.base_url, models


def _valid_url(url: str) -> bool:
    try:
        parsed = URL(url.strip())
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.host)


def _url_error(url: str) -> str | None:
    """Error key for a bad gateway URL: malformed, or (S4) plain http to anything but a local host."""
    if not _valid_url(url):
        return "invalid_url"
    return None if base_url_allowed(url) else "insecure_url"


class HermesVoiceConfigFlow(ConfigFlow, domain=DOMAIN):  # type: ignore[call-arg]
    """Add one Hermes profile (one gateway URL) per entry."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for the gateway URL, API key and display name."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if error := _url_error(user_input[CONF_BASE_URL]):
                errors[CONF_BASE_URL] = error
            else:
                url = normalize_base_url(user_input[CONF_BASE_URL])
                # raise_on_progress=False: an unfinished earlier attempt for the same URL (wrong key,
                # gateway down) must not block this one; HA aborts it once this entry is created.
                await self.async_set_unique_id(url, raise_on_progress=False)
                self._abort_if_unique_id_configured()
                errors, models = await self._try(url, user_input[CONF_API_KEY])
                if not errors:
                    name = user_input.get(CONF_DISPLAY_NAME) or (
                        models[0] if models else DEFAULT_DISPLAY_NAME
                    )
                    return self.async_create_entry(
                        title=name,
                        data={
                            CONF_BASE_URL: url,
                            CONF_API_KEY: user_input[CONF_API_KEY],
                            CONF_DISPLAY_NAME: name,
                            CONF_MODEL: models[0],
                        },
                        options=dict(DEFAULT_OPTIONS),
                    )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(
                    {
                        vol.Required(CONF_BASE_URL): TextSelector(
                            TextSelectorConfig(type=TextSelectorType.URL)
                        ),
                        vol.Required(CONF_API_KEY): TextSelector(
                            TextSelectorConfig(type=TextSelectorType.PASSWORD)
                        ),
                        vol.Optional(CONF_DISPLAY_NAME): str,
                    }
                ),
                user_input or {CONF_BASE_URL: "http://host.docker.internal:8642/v1"},
            ),
            errors=errors,
        )

    async def _try(self, url: str, api_key: str) -> tuple[dict[str, str], list[str]]:
        try:
            _, models = await _validate(self.hass, url, api_key)
        except HermesAuthError:
            return {"base": "invalid_auth"}, []
        except HermesError as err:
            LOGGER.debug("Hermes validation failed: %s", err)
            return {"base": "cannot_connect"}, []
        if not models:
            return {"base": "no_models"}, []
        return {}, models

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Start reauth when the key is rejected."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for a new API key."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            errors, _ = await self._try(entry.data[CONF_BASE_URL], user_input[CONF_API_KEY])
            if not errors:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_API_KEY: user_input[CONF_API_KEY]}
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {vol.Required(CONF_API_KEY): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))}
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> HermesVoiceOptionsFlow:
        """Options flow."""
        return HermesVoiceOptionsFlow()


class HermesVoiceOptionsFlow(OptionsFlowWithReload):
    """Session, voice and error-handling options."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show options."""
        if user_input is not None:
            return self.async_create_entry(data={**DEFAULT_OPTIONS, **user_input})
        current = {**DEFAULT_OPTIONS, **self.config_entry.options}
        schema = vol.Schema(
            {
                vol.Required(CONF_SESSION_MODE): SelectSelector(
                    SelectSelectorConfig(
                        options=SESSION_MODES,
                        mode=SelectSelectorMode.DROPDOWN,
                        translation_key=CONF_SESSION_MODE,
                    )
                ),
                vol.Required(CONF_IDLE_RESET_MINUTES): NumberSelector(
                    NumberSelectorConfig(
                        min=0.05, max=1440, step=0.05, mode=NumberSelectorMode.BOX, unit_of_measurement="min"
                    )
                ),
                vol.Optional(CONF_USER_KEY, default=""): str,
                vol.Required(CONF_VOICE_CLEANUP): BooleanSelector(),
                vol.Optional(CONF_STREAM, default=True): BooleanSelector(),
                vol.Optional(CONF_WORKING_PHRASE, default=""): str,
                vol.Optional(CONF_SEND_TIME, default=True): BooleanSelector(),
                vol.Optional(CONF_AVOID_NAME, default=True): BooleanSelector(),
                vol.Optional(CONF_ALEXA_BRIDGE, default=False): BooleanSelector(),
                vol.Optional(CONF_VOICE_INSTRUCTION, default=""): TextSelector(
                    TextSelectorConfig(multiline=True)
                ),
                vol.Required(CONF_FALLBACK_MESSAGE): str,
                vol.Required(CONF_TIMEOUT): NumberSelector(
                    NumberSelectorConfig(
                        min=5, max=300, step=1, mode=NumberSelectorMode.BOX, unit_of_measurement="s"
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="init", data_schema=self.add_suggested_values_to_schema(schema, current)
        )
