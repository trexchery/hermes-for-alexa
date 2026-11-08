"""Alexa bridge logic with no homeassistant imports (alexa.py wires it to Home Assistant).

Contract: docs/ARCHITECTURE.md. Keys are computed here from ids Alexa sends, never taken from text.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
import re
import time
from typing import Any

from .cleanup import clean_for_voice
from .const import DOMAIN
from .rules import short_hash

MAX_BODY_BYTES = 8 * 1024
MAX_TEXT = 1000
MAX_SPEECH = 6000
DEFAULT_BUDGET_MS = 5500
WINDOW_IDLE_SECONDS = 30 * 60
HOUSEHOLD_SESSION_KEY = "household"  # X-Hermes-Session-Key

KINDS = ("utterance", "resume", "launch", "end")
_EMPTY_TEXT_KINDS = ("resume", "launch", "end")  # ponytail: contract lists resume/launch; "end" has no text either
_FIELDS = {
    "text", "kind", "alexa_device_id", "alexa_user_id", "alexa_person_id",
    "alexa_session_id", "locale", "budget_ms", "has_screen",
}  # fmt: skip
_ID = re.compile(r"^[A-Za-z0-9._:-]+$")
_LOCALE = re.compile(r"^[a-z]{2}-[A-Z]{2}$")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class RequestError(ValueError):
    """The request body broke the contract. The message is safe to send back."""


@dataclass(frozen=True, slots=True)
class AlexaRequest:
    """A validated turn request."""

    text: str
    kind: str
    alexa_device_id: str
    alexa_user_id: str
    alexa_person_id: str | None
    alexa_session_id: str
    locale: str
    budget_ms: int
    has_screen: bool


def _id(body: dict[str, Any], name: str, *, required: bool) -> str | None:
    value = body.get(name)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not 1 <= len(value) <= 512 or not _ID.match(value):
        raise RequestError(f"{name} must be 1-512 characters of A-Z a-z 0-9 . _ : -")
    return value


def validate_request(body: object) -> AlexaRequest:
    """Check a decoded JSON body against the contract; raise RequestError with a safe message."""
    if not isinstance(body, dict):
        raise RequestError("body must be a JSON object")
    unknown = sorted(set(body) - _FIELDS)
    if unknown:
        raise RequestError(f"unknown field: {unknown[0][:40]!r}")
    kind = body.get("kind", "utterance")
    if kind not in KINDS:  # also rejects non-strings
        raise RequestError("kind must be utterance, resume, launch or end")
    text = body.get("text", "")
    if not isinstance(text, str):
        raise RequestError("text must be a string")
    text = text.strip()
    if _CONTROL.search(text):
        raise RequestError("text contains control characters")
    if len(text) > MAX_TEXT or (not text and kind not in _EMPTY_TEXT_KINDS):
        raise RequestError(f"text must be 1-{MAX_TEXT} characters")
    locale = body.get("locale", "en-US")
    if not isinstance(locale, str) or not _LOCALE.match(locale):
        raise RequestError("locale must look like en-US")
    budget = body.get("budget_ms", DEFAULT_BUDGET_MS)
    if type(budget) is not int or not 1000 <= budget <= 7000:
        raise RequestError("budget_ms must be an integer from 1000 to 7000")
    has_screen = body.get("has_screen", False)
    if not isinstance(has_screen, bool):
        raise RequestError("has_screen must be a boolean")
    return AlexaRequest(
        text=text,
        kind=kind,
        alexa_device_id=_id(body, "alexa_device_id", required=True),  # type: ignore[arg-type]
        alexa_user_id=_id(body, "alexa_user_id", required=True),  # type: ignore[arg-type]
        alexa_person_id=_id(body, "alexa_person_id", required=False),
        alexa_session_id=_id(body, "alexa_session_id", required=True),  # type: ignore[arg-type]
        locale=locale,
        budget_ms=budget,
        has_screen=has_screen,
    )


# --- keys ---------------------------------------------------------------------------------------


def device_hash(alexa_device_id: str) -> str:
    """Stable 24-hex id of an Alexa device (the raw id never goes into HA or Hermes)."""
    return short_hash(alexa_device_id, 24)


def device_identifier(alexa_device_id: str) -> tuple[str, str]:
    """Device registry identifier of the HA device that stands for an Alexa device."""
    return (DOMAIN, "alexa:" + device_hash(alexa_device_id))


def speaker_key(person_id: str | None) -> str:
    """``person:<hash>`` for a voice-recognised person, else ``household``."""
    return f"person:{short_hash(person_id, 16)}" if person_id else "household"


def speaker_label(person_id: str | None) -> str:
    """Words for the system message."""
    return "a voice-recognised household member" if person_id else "someone in the household"


def room_slug(room: str | None) -> str:
    """Lowercase letters, digits and dashes of a room name."""
    return re.sub(r"[^a-z0-9]+", "-", (room or "").lower()).strip("-")[:32]


def hermes_session_id(speaker: str, room: str | None, alexa_device_id: str, window_start: int) -> str:
    """``alexa-<speaker>-<room slug or device hash>-<window>`` (charset Hermes accepts)."""
    where = room_slug(room) or device_hash(alexa_device_id)[:12]
    return f"alexa-{speaker}-{where}-{window_start}"


class WindowTracker:
    """Session windows: a window continues while turns arrive less than ``idle`` seconds apart."""

    def __init__(
        self, idle: float = WINDOW_IDLE_SECONDS, clock: Callable[[], float] = time.time, max_keys: int = 1024
    ) -> None:
        """Initialize."""
        self._idle = idle
        self._clock = clock
        self._max = max_keys
        self._windows: dict[str, list[float]] = {}  # key -> [start, last]

    def __len__(self) -> int:
        return len(self._windows)

    def start(self, key: str) -> int:
        """Record a turn for ``key`` and return the start (whole seconds) of its window."""
        now = self._clock()
        for k in [k for k, (_, last) in self._windows.items() if now - last > self._idle]:
            del self._windows[k]
        window = self._windows.get(key)
        if window is None:
            if len(self._windows) >= self._max:  # ponytail: drop the stalest; a cap, not an LRU
                del self._windows[min(self._windows, key=lambda k: self._windows[k][1])]
            window = self._windows[key] = [now, now]
        window[1] = now
        return int(window[0])


# --- text ---------------------------------------------------------------------------------------

_FILLER = {"alexa", "hermes", "hey", "ok", "okay", "please", "thanks", "thank", "you", "now", "well", "so"}
_BYE = {
    "goodbye", "good bye", "bye", "bye bye", "stop", "cancel", "exit", "quit", "good night", "goodnight",
    "that is all", "thats all", "that will be all", "no thanks", "no thank you", "nothing else",
    "i am done", "im done", "we are done", "were done", "never mind", "nevermind", "see you later",
    "talk to you later",
}  # fmt: skip


def _norm(text: str) -> str:
    text = text.lower().replace("’", "'").replace("'", "")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def is_goodbye(text: str) -> bool:
    """An explicit goodbye ("goodbye", "that's all", "no thanks", "stop"), allowing filler like "thanks hermes"."""
    words = _norm(text).split()
    if " ".join(words) in _BYE:
        return True
    while words and words[0] in _FILLER:
        words.pop(0)
    while words and words[-1] in _FILLER:
        words.pop()
    return bool(words) and " ".join(words) in _BYE


_COMMON_ROOM_WORDS = {
    "kitchen", "bedroom", "bathroom", "office", "garage", "hallway", "basement", "attic", "nursery", "den",
    "study", "patio", "porch", "closet", "studio", "foyer", "loft", "lounge", "garden", "balcony", "entryway",
    "playroom", "pantry", "cellar", "sunroom", "workshop", "hall",
}  # fmt: skip
_COMMON_ROOMS = {"living room", "dining room", "family room", "laundry room", "guest room", "game room"}
_LEAD = r"(?:(?:alexa|hermes|hey hermes|ok hermes|okay hermes|please)\s+)*"
_ARTICLE = r"(?:(?:the|my|our)\s+)?"
_ROOM = r"(?P<room>[a-z0-9][a-z0-9' -]*)"
# Explicit about the device and "in": any plausible room name is accepted.
_EXPLICIT = re.compile(
    rf"^{_LEAD}(?:this|that)\s+(?:device|echo|speaker|alexa|dot|one)\s+is\s+(?:located\s+)?(?:in|at)\s+{_ARTICLE}{_ROOM}$"
)
# Everyday phrasing ("this is the kitchen", "you are in the office"): only names that look like rooms,
# or an existing area, so that "this is the best pizza" is a question for Hermes, not a room change.
_LOOSE = re.compile(
    rf"^{_LEAD}(?:(?:this|that)\s+is|you\s+are\s+in|youre\s+in|we\s+are\s+in|were\s+in|"
    rf"(?:set|change)\s+(?:the\s+)?(?:room|location)\s+to)\s+(?:in\s+)?{_ARTICLE}{_ROOM}$"
)
_TRAIL = re.compile(r"\s+(?:please|now|thanks|thank you)$")


def _room_ok(room: str) -> bool:
    words = room.split()
    return 1 <= len(words) <= 3 and len(room) <= 32 and any(c.isalpha() for c in room)


def parse_room_command(text: str, known_rooms: Iterable[str] = ()) -> str | None:
    """Return the room named by "this is the kitchen", "this device is in the living room", "you are in the
    office" and similar, in display case ("Living Room"); None when the text is not a room command."""
    t = text.lower().replace("’", "'").replace("'", "")
    t = " ".join(re.sub(r"[^a-z0-9' -]+", " ", t).split())
    t = _TRAIL.sub("", t)
    if m := _EXPLICIT.match(t):
        room = m["room"].strip(" -")
        loose = False
    elif m := _LOOSE.match(t):
        room = m["room"].strip(" -")
        loose = True
    else:
        return None
    if not _room_ok(room):
        return None
    for known in known_rooms:
        if known.lower() == room:
            return known
    if loose and room not in _COMMON_ROOMS and (room.split()[-1] not in _COMMON_ROOM_WORDS):
        return None
    return " ".join(w.capitalize() for w in room.split())


def _cut(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = text[:limit]
    end = max(head.rfind(". "), head.rfind("! "), head.rfind("? "))
    return head[: end + 1] if end > limit // 2 else head.rstrip()


def finalize_speech(text: str, *, cleanup: bool = True, unsay_name: bool = True) -> str:
    """Voice-clean (the integration's own cleanup) and cap at 6000 characters."""
    out = (clean_for_voice(text, unsay_name) if cleanup else "") or text.strip()
    return _cut(out, MAX_SPEECH)


def join_speech(parts: Iterable[str]) -> str:
    """Join spoken parts with single spaces, within the 6000 character cap."""
    return _cut(" ".join(p.strip() for p in parts if p and p.strip()), MAX_SPEECH)


def build_system_message(voice_instruction: str, room: str | None, time_line: str, label: str) -> str:
    """Voice style, current room, local time and speaker label for Hermes."""
    where = f"The speaker is in the {room}." if room else "The speaker's room is unknown."
    return "\n".join(p for p in (voice_instruction.strip(), where, time_line, f"Speaker: {label}.") if p)


# --- limits and turns ---------------------------------------------------------------------------


class RateLimiter:
    """Sliding window per key: at most ``limit`` calls in ``window`` seconds."""

    def __init__(
        self, limit: int = 30, window: float = 60.0, clock: Callable[[], float] = time.monotonic
    ) -> None:
        """Initialize."""
        self._limit = limit
        self._window = window
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}

    def allow(self, key: str) -> bool:
        """Count a call; False when ``key`` is over the limit."""
        now = self._clock()
        if len(self._hits) > 256:
            for k in [k for k, h in self._hits.items() if not h or now - h[-1] > self._window]:
                del self._hits[k]
        hits = self._hits.setdefault(key, deque())
        while hits and now - hits[0] > self._window:
            hits.popleft()
        if len(hits) >= self._limit:
            return False
        hits.append(now)
        return True


class QueueFull(Exception):
    """Too many turns waiting in one conversation, or too many conversations."""


@dataclass(frozen=True, slots=True)
class TurnResult:
    """What one finished turn will say."""

    speech: str
    source: str


class TurnQueue:
    """One Hermes conversation's turns. They run strictly in order, in the background, and each
    answer is delivered exactly once, in order, to the next request that asks for it."""

    def __init__(self, registry: TurnRegistry) -> None:
        """Initialize."""
        self._reg = registry
        self._turns: list[asyncio.Task[TurnResult]] = []
        self.touched = registry.clock()

    @property
    def busy(self) -> bool:
        """A turn is still running or waiting for its turn."""
        return any(not t.done() for t in self._turns)

    @property
    def has_work(self) -> bool:
        """A turn is running or an answer is waiting to be delivered."""
        return bool(self._turns)

    def submit(self, run: Callable[[], Awaitable[TurnResult]], fallback: str) -> asyncio.Task[TurnResult]:
        """Queue a turn behind the earlier ones; ``fallback`` is said if it fails or times out."""
        if len(self._turns) >= self._reg.max_queue_len:
            raise QueueFull("too many turns waiting")
        prev = self._turns[-1] if self._turns else None
        turn = self._reg.spawn(self._run(run, fallback, prev))
        self._turns.append(turn)
        self.touched = self._reg.clock()
        return turn

    async def _run(
        self, run: Callable[[], Awaitable[TurnResult]], fallback: str, prev: asyncio.Task[TurnResult] | None
    ) -> TurnResult:
        if prev is not None:
            await asyncio.wait({prev})  # never raises the previous turn's error
        async with self._reg.sem:
            try:
                return await asyncio.wait_for(run(), self._reg.turn_timeout)
            except Exception:  # noqa: BLE001  the caller logs inside run(); nothing here may leak out as speech
                return TurnResult(fallback, "hermes")

    async def wait(self, turn: asyncio.Task[TurnResult] | None, timeout: float) -> bool:
        """Wait up to ``timeout`` s for ``turn`` (or, with None, everything queued). True when done.
        Cancelling the caller never cancels the turns."""
        tasks = {t for t in ([turn] if turn else self._turns) if not t.done()}
        if tasks and timeout > 0:
            await asyncio.wait(tasks, timeout=timeout)
        return all(t.done() for t in tasks)

    def take_ready(self) -> list[str]:
        """Speech of the finished, undelivered turns from the front of the queue (marks them delivered)."""
        ready: list[str] = []
        while self._turns and self._turns[0].done():
            turn = self._turns.pop(0)
            if not turn.cancelled():
                ready.append(turn.result().speech)
        self.touched = self._reg.clock()
        return ready

    def cancel(self) -> None:
        """Cancel everything still running."""
        for t in self._turns:
            t.cancel()
        self._turns.clear()


class TurnRegistry:
    """All conversations' queues, with the caps that keep one noisy client from starving the rest."""

    def __init__(
        self,
        *,
        spawn: Callable[[Awaitable[Any]], asyncio.Task[Any]] | None = None,
        clock: Callable[[], float] = time.monotonic,
        max_queue_len: int = 3,
        max_running: int = 3,
        max_queues: int = 64,
        idle_ttl: float = 3600.0,
        turn_timeout: float = 300.0,
    ) -> None:
        """Initialize."""
        self.spawn: Callable[[Awaitable[Any]], asyncio.Task[Any]] = spawn or (lambda c: asyncio.ensure_future(c))
        self.clock = clock
        self.max_queue_len = max_queue_len
        self.max_queues = max_queues
        self.idle_ttl = idle_ttl
        self.turn_timeout = turn_timeout
        self.sem = asyncio.Semaphore(max_running)  # Hermes turns running at once, across all rooms
        self._queues: dict[str, TurnQueue] = {}

    def __len__(self) -> int:
        return len(self._queues)

    def prune(self) -> None:
        """Drop idle queues: empty ones after a minute, ones holding undelivered answers after ``idle_ttl``."""
        now = self.clock()
        for key, q in list(self._queues.items()):
            if q.busy:
                continue
            if now - q.touched > (self.idle_ttl if q.has_work else 60.0):
                q.cancel()
                del self._queues[key]

    def queue(self, key: str) -> TurnQueue:
        """The queue for a Hermes session id, created on first use."""
        self.prune()
        q = self._queues.get(key)
        if q is None:
            if len(self._queues) >= self.max_queues:
                raise QueueFull("too many conversations")
            q = self._queues[key] = TurnQueue(self)
        q.touched = self.clock()
        return q

    def cancel_all(self) -> None:
        """Cancel every running turn and forget every queue."""
        for q in self._queues.values():
            q.cancel()
        self._queues.clear()


class AlexaBridgeState:
    """Everything the bridge keeps between requests."""

    def __init__(self, spawn: Callable[[Awaitable[Any]], asyncio.Task[Any]] | None = None) -> None:
        """Initialize."""
        self.turns = TurnRegistry(spawn=spawn)
        self.limiter = RateLimiter()
        self.windows = WindowTracker()

    def cancel_all(self) -> None:
        """Stop in-flight turns (the integration is unloading)."""
        self.turns.cancel_all()
