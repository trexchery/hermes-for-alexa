r"""Tiny Windows DPAPI secret store (current-user scope). Values are never printed.

Files: %LOCALAPPDATA%\hermes-alexa\secrets\<name>.bin (override the base with HERMES_ALEXA_HOME).
Run deploy modules as `python -m deploy.<name>` from the repo root so this file never shadows stdlib `secrets`.
"""
from __future__ import annotations

import ctypes
import os
import re
import sys
from ctypes import wintypes
from pathlib import Path

ENTROPY = b"hermes-alexa"  # also used by deploy/windows/watchdog.ps1
_NAME = re.compile(r"^[a-z0-9_]{1,64}$")


def app_dir() -> Path:
    base = os.environ.get("HERMES_ALEXA_HOME") or str(Path(os.environ.get("LOCALAPPDATA", Path.home())) / "hermes-alexa")
    return Path(base)


def _path(name: str) -> Path:
    if not _NAME.match(name):
        raise ValueError("secret name must match [a-z0-9_]{1,64}")
    return app_dir() / "secrets" / f"{name}.bin"


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> tuple[_Blob, ctypes.Array]:
    buf = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def _crypt(data: bytes, protect: bool) -> bytes:
    if sys.platform != "win32":
        raise RuntimeError("DPAPI store works on Windows only")
    fn = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
    src, keep1 = _blob(data)
    ent, keep2 = _blob(ENTROPY)
    out = _Blob()
    if protect:
        ok = fn(ctypes.byref(src), "hermes-alexa", ctypes.byref(ent), None, None, 0, ctypes.byref(out))
    else:
        ok = fn(ctypes.byref(src), None, ctypes.byref(ent), None, None, 0, ctypes.byref(out))
    if not ok:
        raise OSError(f"DPAPI call failed (error {ctypes.GetLastError()})")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def put(name: str, value: str) -> None:
    path = _path(name)
    blob = _crypt(value.encode("utf-8"), True)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(blob)
    os.replace(tmp, path)


def get(name: str) -> str:
    path = _path(name)
    if not path.exists():
        raise KeyError(name)
    return _crypt(path.read_bytes(), False).decode("utf-8")


def exists(name: str) -> bool:
    return _path(name).exists()
