"""Diagnostics for Hermes Voice (API key and user key redacted)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import HermesConfigEntry
from .const import CONF_API_KEY, CONF_USER_KEY

TO_REDACT = {CONF_API_KEY, CONF_USER_KEY}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: HermesConfigEntry) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    runtime = getattr(entry, "runtime_data", None)
    return {
        "data": async_redact_data(dict(entry.data), TO_REDACT),
        "options": async_redact_data(dict(entry.options), TO_REDACT),
        "model": runtime.model if runtime else None,
        "per_device_windows": len(runtime.sessions.windows) if runtime else None,
    }
