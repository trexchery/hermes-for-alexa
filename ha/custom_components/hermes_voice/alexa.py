"""Alexa bridge: POST /api/hermes_voice/alexa/v1/turn (contract: docs/ARCHITECTURE.md).

Order for an utterance: room command, then Home Assistant's own agent, then Hermes. The logic that needs
no Home Assistant is in alexa_core.py.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from aiohttp import web
from homeassistant.components import conversation
from homeassistant.components.http import KEY_HASS, HomeAssistantView
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import area_registry as ar, device_registry as dr, intent
from homeassistant.helpers.device_registry import DeviceEntry, DeviceEntryType
from homeassistant.util import dt as dt_util

from .alexa_core import (
    HOUSEHOLD_SESSION_KEY,
    MAX_BODY_BYTES,
    AlexaRequest,
    QueueFull,
    RequestError,
    TurnResult,
    build_system_message,
    device_hash,
    device_identifier,
    finalize_speech,
    hermes_session_id,
    is_goodbye,
    join_speech,
    parse_room_command,
    speaker_key,
    speaker_label,
    validate_request,
)
from .client import HermesError
from .const import (
    CONF_ALEXA_BRIDGE,
    CONF_AVOID_NAME,
    CONF_FALLBACK_MESSAGE,
    CONF_SEND_TIME,
    CONF_TIMEOUT,
    CONF_VOICE_CLEANUP,
    CONF_VOICE_INSTRUCTION,
    DEFAULT_OPTIONS,
    DOMAIN,
    LOGGER,
)
from .rules import time_context

DATA_ALEXA = "hermes_voice_alexa"
MAX_ALEXA_DEVICES = 50  # per config entry: a stolen token cannot fill the device registry
MAX_VOICE_AREAS = 30  # voice room commands create no area beyond this many (a misheard name must not litter HA)
BODY_READ_SECONDS = 10
ALREADY_TOLD = "I already gave you that answer."
HOLDING_LINE = "I'm still working on that. Ask me what I found in a moment."
_LOCAL_MISSES = (intent.IntentResponseErrorCode.NO_INTENT_MATCH, intent.IntentResponseErrorCode.NO_VALID_TARGETS)


class TooManyDevices(Exception):
    """The per-entry cap on Alexa devices was reached."""


def error_response(status: int, code: str, message: str) -> web.Response:
    """The one error shape of the contract."""
    return web.json_response({"error": {"code": code, "message": message}}, status=status)


def bridge_entry(hass: HomeAssistant) -> ConfigEntry | None:
    """The entry that serves the view: one flagged ``alexa_bridge``, else the first loaded one."""
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    flagged = [e for e in entries if e.options.get(CONF_ALEXA_BRIDGE)]
    return (flagged or entries or [None])[0]


def _opt(entry: ConfigEntry, key: str) -> Any:
    return entry.options.get(key, DEFAULT_OPTIONS[key])


# --- Home Assistant registries ------------------------------------------------------------------


def alexa_device(hass: HomeAssistant, entry: ConfigEntry, alexa_device_id: str) -> DeviceEntry:
    """The HA device standing for this Alexa device; created on first sight, with no area."""
    registry = dr.async_get(hass)
    identifier = device_identifier(alexa_device_id)
    if device := registry.async_get_device_by_identifier(identifier, entry.entry_id):
        return device
    known = sum(
        1
        for d in dr.async_entries_for_config_entry(registry, entry.entry_id)
        if any(i[0] == DOMAIN and i[1].startswith("alexa:") for i in d.identifiers)
    )
    if known >= MAX_ALEXA_DEVICES:
        raise TooManyDevices
    return registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={identifier},
        name=f"Alexa {device_hash(alexa_device_id)[:6]}",
        manufacturer="Amazon",
        model="Echo (Alexa skill)",
        entry_type=DeviceEntryType.SERVICE,
    )


def room_of(hass: HomeAssistant, device: DeviceEntry) -> str | None:
    """The room (area name) of a device, if it has one."""
    if device.area_id and (area := ar.async_get(hass).async_get_area(device.area_id)):
        return area.name
    return None


def set_room(hass: HomeAssistant, device: DeviceEntry, room: str) -> str | None:
    """Put the device in the area called ``room`` (created if missing and the cap allows); return the area's
    name, or None when it does not exist and may not be created."""
    areas = ar.async_get(hass)
    area = areas.async_get_area_by_name(room)
    if area is None:
        if len(areas.areas) >= MAX_VOICE_AREAS:
            return None
        area = areas.async_create(room)
    dr.async_get(hass).async_update_device(device.id, area_id=area.id)
    return area.name


# --- the turn -----------------------------------------------------------------------------------


def _reply(status: str, speech: str, source: str, room: str | None, *, end: bool = False) -> dict[str, Any]:
    return {"status": status, "speech": speech, "room": room, "source": source, "end_session": end}


async def _local_answer(
    hass: HomeAssistant, user_id: str, req: AlexaRequest, device: DeviceEntry, timeout: float
) -> str | None:
    """Speech from Home Assistant's own agent, or None when it did not understand (Hermes then answers)."""
    try:
        async with asyncio.timeout(max(timeout, 0.1)):
            result = await conversation.async_converse(
                hass,
                req.text,
                None,
                Context(user_id=user_id),
                language=req.locale,
                agent_id=conversation.HOME_ASSISTANT_AGENT,
                device_id=device.id,
            )
    except TimeoutError:  # the intent may have started: asking Hermes the same command could run it twice
        LOGGER.debug("Local agent timed out for an Alexa turn")
        return "I could not finish that in time."
    except Exception:  # noqa: BLE001  any local failure just means "ask Hermes"
        LOGGER.debug("Local agent failed for an Alexa turn", exc_info=True)
        return None
    response = result.response
    if response.response_type is intent.IntentResponseType.ERROR and response.error_code in _LOCAL_MISSES:
        return None
    speech = (response.speech.get("plain") or {}).get("speech") or ""
    return speech.strip() or "Done."  # it ran (or failed in its own way): never also ask Hermes


def _hermes_turn(entry: ConfigEntry, req: AlexaRequest, session_id: str, room: str | None):
    """A coroutine factory that asks Hermes, run later by the turn queue (strictly after earlier turns)."""
    runtime = entry.runtime_data
    label = speaker_label(req.alexa_person_id)
    fallback = _opt(entry, CONF_FALLBACK_MESSAGE)

    async def run() -> TurnResult:
        time_line = time_context(dt_util.now()) if _opt(entry, CONF_SEND_TIME) else ""
        try:
            reply = await runtime.client.async_chat(
                model=runtime.model,
                user_text=req.text,
                system_prompt=build_system_message(_opt(entry, CONF_VOICE_INSTRUCTION), room, time_line, label),
                session_id=session_id,
                user_key=HOUSEHOLD_SESSION_KEY,
                request_timeout=float(_opt(entry, CONF_TIMEOUT)),
            )
        except HermesError as err:
            LOGGER.warning("Hermes turn failed for an Alexa request (%s): %s", type(err).__name__, err)
            return TurnResult(fallback, "hermes")
        speech = finalize_speech(
            reply.text, cleanup=bool(_opt(entry, CONF_VOICE_CLEANUP)), unsay_name=bool(_opt(entry, CONF_AVOID_NAME))
        )
        return TurnResult(speech or fallback, "hermes")

    return run, fallback


async def handle_turn(
    hass: HomeAssistant, entry: ConfigEntry, user_id: str, req: AlexaRequest
) -> dict[str, Any]:
    """Answer one validated turn."""
    state = hass.data[DATA_ALEXA]
    deadline = time.monotonic() + req.budget_ms / 1000
    device = alexa_device(hass, entry, req.alexa_device_id)
    room = room_of(hass, device)
    if req.kind == "end":  # acknowledge only: undelivered answers stay queued for the next turn
        return _reply("answered", "", "system", room)
    speaker = speaker_key(req.alexa_person_id)
    window = state.windows.start(f"{speaker}|{room or device_hash(req.alexa_device_id)}")
    session_id = hermes_session_id(speaker, room, req.alexa_device_id, window)
    # The queue is keyed by speaker and device only: the room and the window change the Hermes session id, but a
    # pending answer must still be found after "this is the kitchen" or a long pause.
    queue_key = f"{speaker}|{device_hash(req.alexa_device_id)}"
    queue = state.turns.queue(queue_key)

    def remaining() -> float:
        return deadline - time.monotonic()

    if req.kind == "launch":
        ready = queue.take_ready()
        if queue.busy:
            return _reply("pending", join_speech([*ready, "I'm still working on your last question."]), "pending", room)
        return _reply("answered", join_speech(ready) or "Hermes here. What can I do?", "hermes" if ready else "system", room)

    if req.kind == "resume":
        if not queue.has_work:
            return _reply("answered", "I have nothing pending.", "system", room)
        done = await queue.wait(None, remaining())
        ready = queue.take_ready()
        if done:
            return _reply("answered", join_speech(ready) or ALREADY_TOLD, "hermes", room)
        return _reply("pending", join_speech([*ready, HOLDING_LINE]), "pending", room)

    # utterance
    if is_goodbye(req.text):
        return _reply("answered", "Goodbye.", "system", room, end=True)

    areas = [a.name for a in ar.async_get(hass).async_list_areas()]
    if new_room := parse_room_command(req.text, areas):
        if (set_to := set_room(hass, device, new_room)) is None:
            return _reply("answered", join_speech([*queue.take_ready(), "I do not know that room. Create it in Home Assistant first."]), "room", room)
        return _reply("answered", join_speech([*queue.take_ready(), f"Okay, this device is in the {set_to}."]), "room", set_to)

    if (local := await _local_answer(hass, user_id, req, device, remaining())) is not None:
        return _reply("answered", join_speech([*queue.take_ready(), local]), "local", room)

    run, fallback = _hermes_turn(entry, req, session_id, room)
    queue = state.turns.queue(queue_key)  # fetched again: the first one may have been pruned while we waited
    turn = queue.submit(run, fallback)
    done = await queue.wait(turn, remaining())
    ready = queue.take_ready()
    if done:
        return _reply("answered", join_speech(ready) or ALREADY_TOLD, "hermes", room)
    return _reply("pending", join_speech([*ready, HOLDING_LINE]), "pending", room)


class AlexaTurnView(HomeAssistantView):
    """The endpoint the Alexa Lambda calls (HA bearer token of a non-admin user)."""

    url = "/api/hermes_voice/alexa/v1/turn"
    name = "api:hermes_voice:alexa:turn"
    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        """Validate, rate-limit and answer one turn. Errors never carry exception text."""
        hass = request.app[KEY_HASS]
        user = request.get("hass_user")
        if user is None or not user.is_active:
            return error_response(401, "unauthorized", "Not authorized.")
        state = hass.data.get(DATA_ALEXA)
        if state is None or (entry := bridge_entry(hass)) is None:
            return error_response(503, "upstream_unavailable", "Hermes Voice is not loaded.")
        if not state.limiter.allow(user.id):
            return error_response(429, "rate_limited", "Too many requests, slow down.")
        if request.content_type != "application/json":
            return error_response(400, "invalid_request", "Content-Type must be application/json.")
        if (request.content_length or 0) > MAX_BODY_BYTES:
            return error_response(413, "invalid_request", "Body is larger than 8 KiB.")
        try:
            async with asyncio.timeout(BODY_READ_SECONDS):
                body = bytearray()
                while len(body) <= MAX_BODY_BYTES and (chunk := await request.content.read(MAX_BODY_BYTES + 1 - len(body))):
                    body += chunk
        except TimeoutError:
            return error_response(400, "invalid_request", "Request body was not received in time.")
        if len(body) > MAX_BODY_BYTES:
            return error_response(413, "invalid_request", "Body is larger than 8 KiB.")
        try:
            req = validate_request(json.loads(body))
        except RequestError as err:
            return error_response(400, "invalid_request", str(err))
        except (ValueError, RecursionError):
            return error_response(400, "invalid_request", "Body is not valid JSON.")
        try:
            result = await handle_turn(hass, entry, user.id, req)
        except (QueueFull, TooManyDevices):
            return error_response(429, "rate_limited", "Too many conversations or devices in use.")
        except Exception:
            LOGGER.exception("Alexa turn failed")
            return error_response(500, "internal", "Something went wrong.")
        return web.json_response(result)
