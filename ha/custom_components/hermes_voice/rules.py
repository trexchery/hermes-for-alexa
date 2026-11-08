"""Pure rules shared by the integration (no homeassistant imports, unit-testable anywhere)."""

from __future__ import annotations

from datetime import datetime
import hashlib
import ipaddress
import re
from urllib.parse import urlsplit

from .const import MAX_HEADER_LEN

_UNSAFE = re.compile(r"[^A-Za-z0-9_.:-]")
_LOCAL_HOSTS = {"localhost", "host.docker.internal"}


def safe_part(part: str) -> str:
    """Hermes uses session ids in file names: keep them to a conservative charset."""
    return _UNSAFE.sub("_", part)


def short_hash(value: str, length: int) -> str:
    """Hex prefix of sha256(value)."""
    return hashlib.sha256(value.encode()).hexdigest()[:length]


def base_url_allowed(url: str) -> bool:
    """S4: https anywhere; plain http only to loopback, host.docker.internal or a private IP."""
    try:
        parts = urlsplit(url.strip())
        host = (parts.hostname or "").rstrip(".").lower()
    except ValueError:
        return False
    if not host:
        return False
    if parts.scheme == "https":
        return True
    if parts.scheme != "http":
        return False
    if host in _LOCAL_HOSTS:
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or (ip.is_private and not ip.is_unspecified)


def render_user_key(
    template: str, user_id: str | None, device_id: str | None, conversation_id: str
) -> str | None:
    """Expand the user_key option. Empty -> no header. Placeholders: {user_id}, {device_id}.

    S5: a missing user or device never collapses into a shared "anonymous"/"nodevice" scope;
    it becomes a scope of its own for this conversation.
    """
    template = (template or "").strip()
    if not template:
        return None
    own = f"conv-{safe_part(conversation_id)}"
    value = template.replace("{user_id}", user_id or own).replace("{device_id}", device_id or own)
    value = "".join(ch for ch in value if ch.isprintable())
    return value[:MAX_HEADER_LEN] or None


def user_namespace(user_id: str | None) -> str:
    """D2: short stable hash of the HA user id, so one user cannot continue another's Hermes session."""
    return f"u{short_hash(user_id, 12)}" if user_id else "nouser"


def conversation_session_id(conversation_id: str, user_id: str | None) -> str:
    """Session id for per_conversation mode, namespaced by HA user."""
    return f"ha-{user_namespace(user_id)}-{safe_part(conversation_id)}"[:MAX_HEADER_LEN]


def prune_windows(windows: dict[str, dict[str, float]], now: float, idle_seconds: float) -> None:
    """D3: drop device windows idle for longer than the reset time (they would start a new window anyway)."""
    for key in [k for k, w in windows.items() if now - w["last"] > idle_seconds]:
        del windows[key]


def time_context(now: datetime) -> str:
    """One line telling Hermes the local date and time, e.g. for "what time is it?"."""
    hour = now.hour % 12 or 12
    when = f"{now:%A}, {now:%B} {now.day}, {now.year}, {hour}:{now:%M} {'AM' if now.hour < 12 else 'PM'}"
    zone = getattr(now.tzinfo, "key", None) or now.tzname() or "local time"
    return f"Current local date and time: {when} ({zone})."
