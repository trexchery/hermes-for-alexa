"""Constants for Hermes Voice."""

from __future__ import annotations

import logging
from typing import Final

DOMAIN: Final = "hermes_voice"
LOGGER = logging.getLogger(__package__)

CONF_BASE_URL: Final = "base_url"
CONF_API_KEY: Final = "api_key"
CONF_DISPLAY_NAME: Final = "display_name"
CONF_MODEL: Final = "model"

CONF_SESSION_MODE: Final = "session_mode"
CONF_IDLE_RESET_MINUTES: Final = "idle_reset_minutes"
CONF_USER_KEY: Final = "user_key"
CONF_VOICE_CLEANUP: Final = "voice_cleanup"
CONF_VOICE_INSTRUCTION: Final = "voice_instruction"
CONF_FALLBACK_MESSAGE: Final = "fallback_message"
CONF_TIMEOUT: Final = "timeout"
CONF_STREAM: Final = "stream"
CONF_WORKING_PHRASE: Final = "working_phrase"
CONF_SEND_TIME: Final = "send_time"
CONF_AVOID_NAME: Final = "avoid_name"
CONF_ALEXA_BRIDGE: Final = "alexa_bridge"

SESSION_PER_CONVERSATION: Final = "per_conversation"
SESSION_PER_DEVICE: Final = "per_device"
SESSION_MODES: Final = [SESSION_PER_CONVERSATION, SESSION_PER_DEVICE]

DEFAULT_DISPLAY_NAME: Final = "Hermes"
DEFAULT_SESSION_MODE: Final = SESSION_PER_CONVERSATION
DEFAULT_IDLE_RESET_MINUTES: Final = 30
DEFAULT_USER_KEY: Final = ""
DEFAULT_VOICE_CLEANUP: Final = True
DEFAULT_VOICE_INSTRUCTION: Final = (
    "You are being heard through a voice speaker: answer in one to three short "
    "spoken sentences, without lists, markdown, links or emoji."
)
DEFAULT_FALLBACK_MESSAGE: Final = "I can't reach my brain right now, try again in a minute."
# Total limit for one turn. Long tool runs need minutes; the connect and silence limits are in client.py.
DEFAULT_TIMEOUT: Final = 120
DEFAULT_STREAM: Final = True
# Spoken when Hermes starts a tool (web search, ...) before it has said anything.
DEFAULT_WORKING_PHRASE: Final = "One moment."
# Hermes has no clock: tell it Home Assistant's local date and time on every turn.
DEFAULT_SEND_TIME: Final = True
# Replies do not say the assistant's name aloud: an Echo with microWakeWord hears itself and wakes.
DEFAULT_AVOID_NAME: Final = True
# This entry serves the Alexa bridge view. Off: the first loaded entry serves it unless another one is flagged.
DEFAULT_ALEXA_BRIDGE: Final = False

DEFAULT_OPTIONS: Final = {
    CONF_SESSION_MODE: DEFAULT_SESSION_MODE,
    CONF_IDLE_RESET_MINUTES: DEFAULT_IDLE_RESET_MINUTES,
    CONF_USER_KEY: DEFAULT_USER_KEY,
    CONF_VOICE_CLEANUP: DEFAULT_VOICE_CLEANUP,
    CONF_VOICE_INSTRUCTION: DEFAULT_VOICE_INSTRUCTION,
    CONF_FALLBACK_MESSAGE: DEFAULT_FALLBACK_MESSAGE,
    CONF_TIMEOUT: DEFAULT_TIMEOUT,
    CONF_STREAM: DEFAULT_STREAM,
    CONF_WORKING_PHRASE: DEFAULT_WORKING_PHRASE,
    CONF_SEND_TIME: DEFAULT_SEND_TIME,
    CONF_AVOID_NAME: DEFAULT_AVOID_NAME,
    CONF_ALEXA_BRIDGE: DEFAULT_ALEXA_BRIDGE,
}

# Hermes API server headers (hermes-agent gateway/platforms/api_server_openai_routes.py).
HEADER_SESSION_ID: Final = "X-Hermes-Session-Id"
HEADER_SESSION_KEY: Final = "X-Hermes-Session-Key"
HEADER_COMPLETED: Final = "X-Hermes-Completed"
HEADER_ERROR: Final = "X-Hermes-Error"
MAX_HEADER_LEN: Final = 256

STORAGE_VERSION: Final = 1
STORAGE_KEY_PREFIX: Final = "hermes_voice.device_windows"
