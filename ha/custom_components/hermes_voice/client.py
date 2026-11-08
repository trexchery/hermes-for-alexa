"""Minimal async client for the Hermes Agent OpenAI-compatible API server."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
import json
from typing import Any

import aiohttp

from .const import HEADER_COMPLETED, HEADER_ERROR, HEADER_SESSION_ID, HEADER_SESSION_KEY


class HermesError(Exception):
    """Base error talking to Hermes."""


class HermesAuthError(HermesError):
    """API key rejected (401/403)."""


class HermesRateLimitError(HermesError):
    """Hermes or its upstream model provider is rate limited (429)."""


class HermesTimeoutError(HermesError):
    """Request took longer than the configured timeout."""


class HermesConnectionError(HermesError):
    """Gateway unreachable or returned an unexpected HTTP status."""


class HermesAgentError(HermesError):
    """The agent run failed (Hermes reported failed / no usable text)."""


# D4: how long to wait to connect, and how long the gateway may stay silent (Hermes sends a keepalive
# comment every 10 s while a streamed run is busy). The caller's request_timeout is the total limit.
CONNECT_TIMEOUT = 5.0
FIRST_BYTE_TIMEOUT = 30.0

_RATE_LIMIT_MARKERS = ("429", "rate limit", "rate-limit", "rate_limit", "usage limit")


def normalize_base_url(url: str) -> str:
    """Return the base URL ending in /v1 without a trailing slash.

    Accepts e.g. ``http://host:8642``, ``http://host:8642/v1/`` or
    ``http://host:8642/p/voice`` (multiplexed profile route).
    """
    url = url.strip().rstrip("/")
    if not url.endswith("/v1"):
        url = f"{url}/v1"
    return url


# Hermes answers a provider login or quota failure with a normal-looking reply that starts with one of
# these labels (hermes-agent gateway/platforms/api_server.py, PROVIDER_*_LABEL). Never speak it.
_PROVIDER_AUTH_LABEL = "provider authentication failed"
_PROVIDER_RATE_LABEL = "provider rate-limited"


def _raise_if_provider_failure(text: str) -> None:
    head = text.lstrip().lower()
    if head.startswith(_PROVIDER_RATE_LABEL):
        raise HermesRateLimitError("provider rate-limited")
    if head.startswith(_PROVIDER_AUTH_LABEL):
        raise HermesAgentError("provider authentication failed (check the profile's model login)")


def _looks_rate_limited(text: Any) -> bool:
    low = text.lower() if isinstance(text, str) else ""
    return any(marker in low for marker in _RATE_LIMIT_MARKERS)


@dataclass(slots=True)
class HermesReply:
    """A successful agent reply."""

    text: str
    session_id: str | None


@dataclass(slots=True)
class TextDelta:
    """A piece of the agent's reply, streamed as it is written."""

    text: str


@dataclass(slots=True)
class ToolEvent:
    """Hermes started or finished one of its own tools (web search, ...)."""

    tool_call_id: str
    tool: str
    label: str
    running: bool


StreamEvent = TextDelta | ToolEvent


def _raise_for_error(status: int, data: Any, hermes_error: str | None) -> None:
    """Map a non-200 Hermes response to an exception."""
    if not isinstance(data, dict):
        data = {}
    if status in (401, 403):
        raise HermesAuthError(f"HTTP {status}")
    raw_error = data.get("error")
    error_obj: dict[str, Any] = raw_error if isinstance(raw_error, dict) else {}
    if status == 429 or (status >= 400 and _looks_rate_limited(error_obj.get("message"))):
        raise HermesRateLimitError(f"HTTP {status}")
    if status >= 500 and status != 502:
        raise HermesConnectionError(f"HTTP {status}")
    if status == 502:
        # Hermes' "agent_incomplete" envelope: the run failed without usable text.
        if _looks_rate_limited(error_obj.get("message", "")) or _looks_rate_limited(hermes_error):
            raise HermesRateLimitError("agent failed: rate limited")
        raise HermesAgentError("agent failed")
    if status != 200:
        raise HermesConnectionError(f"HTTP {status}")


def _raise_if_failed(extras: Any, finish_reason: Any, error: Any, hermes_error: str | None) -> None:
    """Hermes may finish with HTTP 200 and a failed run (often the provider's error text)."""
    extras = extras if isinstance(extras, dict) else {}
    err_msg = error.get("message") if isinstance(error, dict) else None
    if bool(extras.get("failed")) or finish_reason == "error":
        if any(_looks_rate_limited(t) for t in (extras.get("error"), err_msg, hermes_error)):
            raise HermesRateLimitError("agent failed: rate limited")
        raise HermesAgentError("agent failed")


class HermesClient:
    """Talks to one Hermes profile's API server."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        api_key: str,
        *,
        connect_timeout: float = CONNECT_TIMEOUT,
        first_byte_timeout: float = FIRST_BYTE_TIMEOUT,
    ) -> None:
        """Initialize the client."""
        self._session = session
        self.base_url = normalize_base_url(base_url)
        self._api_key = api_key
        self._connect_timeout = connect_timeout
        self._first_byte_timeout = first_byte_timeout

    def _timeout(self, total: float, *, streaming: bool) -> aiohttp.ClientTimeout:
        """D4: connect, silence (headers, first byte, gaps; Hermes keepalives every 10 s) and total are separate.

        A non-streaming reply is silent until the run ends, so only the total limit applies to it.
        """
        silence = min(self._first_byte_timeout, total) if streaming else total
        return aiohttp.ClientTimeout(total=total, sock_connect=min(self._connect_timeout, total), sock_read=silence)

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"}

    async def async_list_models(self, request_timeout: float = 10) -> list[str]:
        """Return advertised model ids (no model call is made)."""
        try:
            async with self._session.get(
                f"{self.base_url}/models",
                headers=self._headers,
                timeout=aiohttp.ClientTimeout(total=request_timeout),
            ) as resp:
                if resp.status in (401, 403):
                    raise HermesAuthError(f"HTTP {resp.status}")
                if resp.status != 200:
                    raise HermesConnectionError(f"HTTP {resp.status}")
                data = await resp.json(content_type=None)
        except HermesError:
            raise
        except TimeoutError as err:
            raise HermesTimeoutError("timeout listing models") from err
        except (aiohttp.ClientError, ValueError) as err:
            raise HermesConnectionError(str(err)) from err
        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise HermesAgentError("malformed model list")
        return [m["id"] for m in items if isinstance(m, dict) and isinstance(m.get("id"), str)]

    def _request(
        self,
        model: str,
        user_text: str,
        system_prompt: str | None,
        session_id: str,
        user_key: str | None,
        *,
        stream: bool,
    ) -> tuple[dict[str, str], dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_text})
        headers = {**self._headers, HEADER_SESSION_ID: session_id}
        if user_key:
            headers[HEADER_SESSION_KEY] = user_key
        return headers, {"model": model, "messages": messages, "stream": stream}

    async def async_chat_stream(
        self,
        *,
        model: str,
        user_text: str,
        system_prompt: str | None,
        session_id: str,
        user_key: str | None,
        request_timeout: float,
    ) -> AsyncIterator[StreamEvent]:
        """Run one agent turn and yield the reply as it is written, plus Hermes' tool events.

        Raises the same errors as async_chat; they can come after some text was yielded.
        A server that answers with plain JSON instead of an event stream is handled too.
        """
        headers, body = self._request(model, user_text, system_prompt, session_id, user_key, stream=True)
        got_text = False
        try:
            async with self._session.post(
                f"{self.base_url}/chat/completions",
                json=body,
                headers=headers,
                timeout=self._timeout(request_timeout, streaming=True),
            ) as resp:
                hermes_error = resp.headers.get(HEADER_ERROR)
                if "text/event-stream" not in (resp.content_type or ""):
                    try:
                        data = await resp.json(content_type=None)
                    except ValueError:
                        data = {}
                    _raise_for_error(resp.status, data, hermes_error)
                    reply = self._reply_from_json(
                        data, hermes_error, resp.headers.get(HEADER_COMPLETED), None
                    )
                    yield TextDelta(reply.text)
                    return
                _raise_for_error(resp.status, {}, hermes_error)
                event = ""
                async for raw in resp.content:
                    line = raw.decode("utf-8", "replace").rstrip("\r\n")
                    if not line:
                        event = ""
                        continue
                    if line.startswith(":"):
                        continue  # keepalive
                    if line.startswith("event:"):
                        event = line[6:].strip()
                        continue
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        frame = json.loads(payload)
                    except ValueError:
                        continue
                    if not isinstance(frame, dict):
                        continue
                    if event == "hermes.tool.progress":
                        status = frame.get("status")
                        if status in ("running", "completed") and frame.get("toolCallId"):
                            yield ToolEvent(
                                tool_call_id=str(frame["toolCallId"]),
                                tool=str(frame.get("tool") or "tool"),
                                label=str(frame.get("label") or frame.get("tool") or ""),
                                running=status == "running",
                            )
                        continue
                    if event:
                        continue  # other Hermes events (status, approval) are not spoken
                    choices = frame.get("choices")
                    choice = choices[0] if isinstance(choices, list) and choices else None
                    choice = choice if isinstance(choice, dict) else {}
                    delta = choice.get("delta")
                    delta = delta if isinstance(delta, dict) else {}
                    finish = choice.get("finish_reason")
                    if finish is not None:
                        # Same failure check as the JSON path: only a failed run
                        # (hermes.failed, or finish_reason "error") fails the turn.
                        # A benign finish such as "length" keeps the text already
                        # streamed instead of discarding it as a failure.
                        _raise_if_failed(frame.get("hermes") or {}, finish, frame.get("error"), hermes_error)
                    text = delta.get("content")
                    if isinstance(text, str) and text:
                        if not got_text:
                            _raise_if_provider_failure(text)
                        got_text = True
                        yield TextDelta(text)
        except TimeoutError as err:
            raise HermesTimeoutError(f"no reply within {request_timeout}s") from err
        except aiohttp.ClientError as err:
            raise HermesConnectionError(str(err)) from err
        except ValueError as err:  # e.g. an event line longer than the reader limit
            raise HermesAgentError("malformed stream") from err
        if not got_text:
            raise HermesAgentError("empty reply")

    async def async_chat(
        self,
        *,
        model: str,
        user_text: str,
        system_prompt: str | None,
        session_id: str,
        user_key: str | None,
        request_timeout: float,
    ) -> HermesReply:
        """Run one agent turn. Only the new user message is sent; Hermes holds history."""
        headers, body = self._request(model, user_text, system_prompt, session_id, user_key, stream=False)
        try:
            async with self._session.post(
                f"{self.base_url}/chat/completions",
                json=body,
                headers=headers,
                timeout=self._timeout(request_timeout, streaming=False),
            ) as resp:
                status = resp.status
                hermes_error = resp.headers.get(HEADER_ERROR)
                completed = resp.headers.get(HEADER_COMPLETED)
                returned_sid = resp.headers.get(HEADER_SESSION_ID)
                try:
                    data = await resp.json(content_type=None)
                except ValueError:
                    data = {}
        except TimeoutError as err:
            raise HermesTimeoutError(f"no reply within {request_timeout}s") from err
        except aiohttp.ClientError as err:
            raise HermesConnectionError(str(err)) from err

        _raise_for_error(status, data, hermes_error)
        return self._reply_from_json(data, hermes_error, completed, returned_sid)

    @staticmethod
    def _reply_from_json(
        data: Any, hermes_error: str | None, completed: str | None, returned_sid: str | None
    ) -> HermesReply:
        if not isinstance(data, dict):
            data = {}
        try:
            choice = data["choices"][0]
            text = choice["message"].get("content") or ""
            finish_reason = choice.get("finish_reason")
        except (KeyError, IndexError, TypeError, AttributeError) as err:
            raise HermesAgentError("malformed response") from err
        if not isinstance(text, str):
            raise HermesAgentError("malformed response")

        _raise_if_failed(data.get("hermes"), finish_reason, data.get("error"), hermes_error)
        if completed == "false" and _looks_rate_limited(hermes_error):
            raise HermesRateLimitError("agent incomplete: rate limited")
        if not text.strip():
            raise HermesAgentError("empty reply")
        _raise_if_provider_failure(text)
        return HermesReply(text=text, session_id=returned_sid)
