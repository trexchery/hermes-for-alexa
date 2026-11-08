"""Turn agent text into something a TTS engine can read aloud."""

from __future__ import annotations

import re

_CODE_FENCE = re.compile(r"```.*?(```|$)", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`]*)`")
_MD_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_MD_LINK = re.compile(r"\[([^\]]+)\]\((?:[^()]|\([^)]*\))*\)")
_URL = re.compile(r"(?:https?://|www\.)[^\s<>()\[\]]+[^\s<>()\[\].,;:!?'\"]", re.IGNORECASE)
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*")
_QUOTE = re.compile(r"^\s{0,3}>\s?")
_BULLET = re.compile(r"^\s*(?:[-*+•]|\d+[.)])\s+")
_HR = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
_EMPHASIS = re.compile(r"(\*\*|__|\*|_|~~)(?=\S)(.+?)(?<=\S)\1")
_HTML_TAG = re.compile(r"</?[A-Za-z][^>]*>")
_EMOJI = re.compile(
    "["
    "\U0001f000-\U0001faff"  # pictographs, emoticons, transport, symbols & pictographs ext.
    "\U00002600-\U000027bf"  # misc symbols, dingbats
    "\U0001f1e6-\U0001f1ff"  # flags
    "\U00002b00-\U00002bff"  # arrows / stars
    "\U0000fe0f\U0000200d\U000020e3"  # variation selector, ZWJ, keycap
    "]+"
)
_SPACES = re.compile("[ \t\u00a0]+")


def _table_row(line: str) -> str | None:
    stripped = line.strip()
    if stripped.count("|") >= 2:
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        cells = [c for c in cells if c]
        return ", ".join(cells) if cells else None
    return None


def clean_for_voice(text: str, unsay_name: bool = True) -> str:
    """Strip markdown, code, URLs, emoji and tables; collapse whitespace.

    Code blocks are dropped (they cannot be read aloud). URLs become the word "link"
    (a markdown link keeps its label). Table rows become comma-separated sentences.
    """
    if not text:
        return ""
    text = text.replace("\r\n", "\n")
    text = _CODE_FENCE.sub(" ", text)
    text = _MD_IMAGE.sub(lambda m: m.group(1), text)
    text = _MD_LINK.sub(lambda m: m.group(1), text)
    text = _URL.sub("link", text)
    text = _INLINE_CODE.sub(lambda m: m.group(1), text)
    text = _HTML_TAG.sub("", text)

    sentences: list[str] = []
    for raw in text.split("\n"):
        line = raw.strip()
        if not line or _TABLE_SEP.match(line) or _HR.match(line):
            sentences.append("")
            continue
        row = _table_row(line)
        if row is not None:
            line = row
        line = _HEADING.sub("", line)
        line = _QUOTE.sub("", line)
        line = _BULLET.sub("", line)
        sentences.append(line)

    # Join lines: blank lines / list items become sentence breaks.
    out: list[str] = []
    for line in sentences:
        if not line:
            continue
        if out and out[-1][-1:] not in ".!?:;,":
            out[-1] += "."
        out.append(line)
    text = " ".join(out)

    for _ in range(2):  # nested emphasis
        text = _EMPHASIS.sub(lambda m: m.group(2), text)
    text = _EMOJI.sub("", text)
    text = text.replace("|", " ")
    text = _SPACES.sub(" ", text)
    text = re.sub(r"\s+([.,!?;:])", r"\1", text)
    text = re.sub(r"([.!?])\.+", r"\1", text)
    return unsay_wake_word(text.strip()) if unsay_name else text.strip()


# The wake word's name, said aloud by the speaker itself, wakes it: microWakeWord on an Echo (echo-dot-assist)
# keeps listening while the Echo speaks and has no echo suppression for its own voice. Seen on an Echo Show 5:
# "...the latest Hermes Agent release..." started a new turn in the middle of the reply. The spoken text says
# something else; the text Hermes wrote keeps the real words.
_NAME_SWAPS = (
    (re.compile(r"\b(?:hey|okay|ok) hermes\b", re.IGNORECASE), "the wake word"),
    (re.compile(r"\bhermes voice\b", re.IGNORECASE), "the voice setup"),
    # after "the", "latest", ... just "agent": "the latest Hermes Agent release" -> "the latest agent release"
    (
        re.compile(
            r"\b(the|this|that|a|an|latest|newest|new|old|your|my|our)\s+hermes(?:\s+agent)?\b", re.IGNORECASE
        ),
        r"\1 agent",
    ),
    (re.compile(r"\bhermes agent\b", re.IGNORECASE), "the agent"),
    (re.compile(r"\bhermes(?:['\u2019]s|['\u2019])", re.IGNORECASE), "the agent's"),
    (re.compile(r"\bhermes\b", re.IGNORECASE), "the agent"),
)


def unsay_wake_word(text: str) -> str:
    """Replace the assistant's own name in text that will be spoken (see _NAME_SWAPS)."""
    for pattern, spoken in _NAME_SWAPS:
        text = pattern.sub(spoken, text)
    # "the agent" at the start of a sentence gets its capital back.
    return re.sub(
        r"(^|[.!?]\s+)(the (?:agent|voice setup|wake word))",
        lambda m: m.group(1) + m.group(2)[0].upper() + m.group(2)[1:],
        text,
    )


# Sentence ends and paragraph breaks. Single line breaks are left to clean_for_voice, which
# joins list items and table rows into sentences.
_BOUNDARY = re.compile(r"[.!?](?=\s)|\n\s*\n")


class VoiceChunker:
    """Collect streamed text and hand it on in whole sentences.

    TTS gets complete sentences, and markdown is cleaned per sentence. Pieces carry no
    surrounding spaces; the caller joins them. Text inside an open code
    fence is held back until the fence closes (it is then dropped by the cleanup). With cleanup off,
    text is passed on unchanged as it arrives.
    """

    def __init__(self, cleanup: bool, unsay_name: bool = True) -> None:
        """Initialize."""
        self._cleanup = cleanup
        self._unsay_name = unsay_name
        self._buf = ""

    def _emit(self, text: str) -> str:
        cleaned = clean_for_voice(text, self._unsay_name)
        if cleaned and text.rstrip(" \t").endswith("\n") and cleaned[-1] not in ".!?:;,":
            cleaned += "."  # a heading or list item before a paragraph break
        return cleaned

    def feed(self, text: str) -> str:
        """Add streamed text; return what is ready to be spoken ('' if nothing yet)."""
        if not self._cleanup:
            return text
        self._buf += text
        searchable = self._buf
        if self._buf.count("```") % 2:
            searchable = self._buf[: self._buf.rindex("```")]
        cut = 0
        for match in _BOUNDARY.finditer(searchable):
            cut = match.end()
        if not cut:
            return ""
        ready, self._buf = self._buf[:cut], self._buf[cut:].lstrip(" ")
        return self._emit(ready)

    def flush(self) -> str:
        """Return whatever is left."""
        ready, self._buf = self._buf, ""
        return self._emit(ready)
