"""Fixtures for the Home Assistant tests (run with ha/tests/run.sh, not in the plain-pytest CI)."""

from unittest.mock import AsyncMock, patch

from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry
import pytest

from custom_components.hermes_voice.client import HermesClient, HermesReply
from custom_components.hermes_voice.const import DOMAIN


@pytest.fixture(autouse=True)
def _custom_integrations(enable_custom_integrations):
    """Let Home Assistant load custom_components/ from ha/."""


@pytest.fixture(autouse=True)
async def _core_component(hass):
    """The conversation component expects the always-present ``homeassistant`` component (exposed entities)."""
    assert await async_setup_component(hass, "homeassistant", {})


@pytest.fixture
def chat():
    """Replace the Hermes HTTP calls; ``chat`` is the async_chat mock."""
    with (
        patch.object(HermesClient, "async_list_models", AsyncMock(return_value=["hermes-agent"])),
        patch.object(HermesClient, "async_chat", AsyncMock(return_value=HermesReply("Hello there.", None))) as mock,
    ):
        yield mock


def make_entry(hass, **options) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "base_url": "http://host.docker.internal:8642/v1",
            "api_key": "k" * 24,
            "display_name": "Hermes",
            "model": "hermes-agent",
        },
        options=options,
        unique_id="http://host.docker.internal:8642/v1",
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
async def entry(hass, chat):
    """A loaded Hermes Voice entry."""
    e = make_entry(hass, stream=False)
    assert await hass.config_entries.async_setup(e.entry_id)
    await hass.async_block_till_done()
    return e
