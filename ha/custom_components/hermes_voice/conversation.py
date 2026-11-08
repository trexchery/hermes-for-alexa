"""Conversation agent backed by a Hermes profile."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import Any, Literal

from homeassistant.components import conversation
from homeassistant.const import MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import HermesConfigEntry
from .cleanup import VoiceChunker, clean_for_voice
from .client import HermesError, HermesRateLimitError, HermesTimeoutError, TextDelta, ToolEvent
from .const import (
    CONF_AVOID_NAME,
    CONF_DISPLAY_NAME,
    CONF_FALLBACK_MESSAGE,
    CONF_IDLE_RESET_MINUTES,
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
)
from .rules import render_user_key, time_context

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HermesConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the conversation entity."""
    async_add_entities([HermesConversationEntity(entry)])


class HermesConversationEntity(conversation.ConversationEntity):
    """Send each turn to Hermes with a stable session id."""

    _attr_has_entity_name = True
    _attr_name = None

    def __init__(self, entry: HermesConfigEntry) -> None:
        """Initialize."""
        self.entry = entry
        self._attr_unique_id = entry.entry_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.data.get(CONF_DISPLAY_NAME, DEFAULT_DISPLAY_NAME),
            manufacturer="Nous Research",
            model="Hermes Agent profile",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def supports_streaming(self) -> bool:
        """Streaming lets Home Assistant start speaking before the whole reply is written."""
        return bool(self._opt(CONF_STREAM))

    @property
    def supported_languages(self) -> list[str] | Literal["*"]:
        """Hermes handles any language its model handles."""
        return MATCH_ALL

    def _opt(self, key: str) -> Any:
        return self.entry.options.get(key, DEFAULT_OPTIONS[key])

    async def async_added_to_hass(self) -> None:
        """Register as an agent."""
        await super().async_added_to_hass()
        conversation.async_set_agent(self.hass, self.entry, self)

    async def async_will_remove_from_hass(self) -> None:
        """Unregister."""
        conversation.async_unset_agent(self.hass, self.entry)
        await super().async_will_remove_from_hass()

    async def _async_handle_message(
        self,
        user_input: conversation.ConversationInput,
        chat_log: conversation.ChatLog,
    ) -> conversation.ConversationResult:
        """Send the new user message to Hermes and return its reply."""
        data = self.entry.runtime_data
        user_id = user_input.context.user_id
        session_id = await data.sessions.async_resolve(
            mode=self._opt(CONF_SESSION_MODE),
            conversation_id=chat_log.conversation_id,
            device_id=user_input.device_id,
            user_id=user_id,
            idle_reset_minutes=float(self._opt(CONF_IDLE_RESET_MINUTES)),
        )
        system_parts = [self._opt(CONF_VOICE_INSTRUCTION) or ""]
        if self._opt(CONF_SEND_TIME):
            system_parts.append(time_context(dt_util.now()))
        if user_input.extra_system_prompt:
            system_parts.append(user_input.extra_system_prompt)
        system_prompt = "\n".join(p for p in system_parts if p.strip()) or None

        request = {
            "model": data.model,
            "user_text": user_input.text,
            "system_prompt": system_prompt,
            "session_id": session_id,
            "user_key": render_user_key(
                self._opt(CONF_USER_KEY), user_id, user_input.device_id, chat_log.conversation_id
            ),
            "request_timeout": float(self._opt(CONF_TIMEOUT)),
        }
        if self._opt(CONF_STREAM):
            async for _content in chat_log.async_add_delta_content_stream(
                user_input.agent_id, self._stream_deltas(request, session_id)
            ):
                pass
            return conversation.async_get_result_from_chat_log(user_input, chat_log)

        try:
            reply = await data.client.async_chat(**request)
            text = reply.text
            if self._opt(CONF_VOICE_CLEANUP):
                text = clean_for_voice(text, bool(self._opt(CONF_AVOID_NAME))) or text.strip()
        except HermesError as err:
            self._log_failure(err, session_id)
            text = self._opt(CONF_FALLBACK_MESSAGE)

        chat_log.async_add_assistant_content_without_tools(
            conversation.AssistantContent(agent_id=user_input.agent_id, content=text)
        )
        return conversation.async_get_result_from_chat_log(user_input, chat_log)

    def _log_failure(self, err: HermesError, session_id: str) -> None:
        kind = (
            "rate limited"
            if isinstance(err, HermesRateLimitError)
            else "timeout"
            if isinstance(err, HermesTimeoutError)
            else type(err).__name__
        )
        LOGGER.warning("Hermes turn failed (%s, session %s): %s", kind, session_id, err)

    async def _stream_deltas(
        self, request: dict[str, Any], session_id: str
    ) -> AsyncIterator[conversation.AssistantContentDeltaDict | conversation.ToolResultContentDeltaDict]:
        """Turn Hermes' stream into chat log deltas.

        Tools Hermes runs itself are recorded as external tool calls. When a tool starts before
        anything was said, the working phrase is spoken first; text followed by a tool call makes
        the Assist pipeline start text-to-speech right away instead of after the whole reply.
        """
        whole_sentences = bool(self._opt(CONF_VOICE_CLEANUP))  # the chunker hands on whole sentences
        chunker = VoiceChunker(whole_sentences, bool(self._opt(CONF_AVOID_NAME)))
        working_phrase = (self._opt(CONF_WORKING_PHRASE) or "").strip()
        role: str = "assistant"
        said_anything = False  # in this whole turn
        answer_text = False  # in the current assistant message
        open_tools: set[str] = set()
        yield {"role": "assistant"}

        def assistant() -> list[conversation.AssistantContentDeltaDict]:
            nonlocal role, answer_text
            if role == "assistant":
                return []
            role, answer_text = "assistant", False
            return [{"role": "assistant"}]

        def say(text: str) -> conversation.AssistantContentDeltaDict:
            """Content for the current assistant message, one sentence (or chunk) at a time.

            Each piece ends with a blank line. Piper's sentence splitter (Wyoming Piper's
            SentenceBoundaryDetector) treats a sentence as finished only when the next one starts,
            or at a blank line; without it every sentence waited for the next, so speech ran a
            sentence or two behind the text and "One moment." stayed silent until the tool returned.
            """
            nonlocal said_anything, answer_text
            if not whole_sentences:  # voice cleanup off: raw text, as it streams
                sep = " " if answer_text and not text[:1].isspace() else ""
                said_anything = answer_text = True
                return {"content": sep + text}
            said_anything = answer_text = True
            return {"content": text.strip() + "\n\n"}

        try:
            # aclosing: when HA gives up (cancel, early exit) the HTTP request to Hermes is closed at once (D4).
            async with aclosing(self.entry.runtime_data.client.async_chat_stream(**request)) as events:
                async for event in events:
                    if isinstance(event, TextDelta):
                        if piece := chunker.feed(event.text):
                            for d in assistant():
                                yield d
                            yield say(piece)
                    elif isinstance(event, ToolEvent) and event.running:
                        for d in assistant():
                            yield d
                        if piece := chunker.flush():
                            yield say(piece)
                        if not said_anything and working_phrase:
                            yield say(working_phrase)
                        open_tools.add(event.tool_call_id)
                        yield {
                            "tool_calls": [
                                llm.ToolInput(
                                    id=event.tool_call_id,
                                    tool_name=event.tool,
                                    tool_args={"label": event.label},
                                    external=True,
                                )
                            ]
                        }
                    elif isinstance(event, ToolEvent) and event.tool_call_id in open_tools:
                        open_tools.discard(event.tool_call_id)
                        role = "tool_result"
                        yield {
                            "role": "tool_result",
                            "tool_call_id": event.tool_call_id,
                            "tool_name": event.tool,
                            "result": llm.ToolResult(data={"status": "completed"}),
                        }
            if piece := chunker.flush():
                for d in assistant():
                    yield d
                yield say(piece)
        except HermesError as err:
            self._log_failure(err, session_id)
            for d in assistant():
                yield d
            yield say(self._opt(CONF_FALLBACK_MESSAGE))
        if not answer_text:
            # The turn must end with something to say (e.g. Hermes stopped right after a tool).
            for d in assistant():
                yield d
            yield say(self._opt(CONF_FALLBACK_MESSAGE))
