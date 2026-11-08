"""Import hermes_voice's pure modules without Home Assistant: the package __init__ is never run."""

from __future__ import annotations

import asyncio
import functools
import importlib
from pathlib import Path
import sys
import types

_NAME = "hermes_voice_pure"
_DIR = Path(__file__).resolve().parents[2] / "ha" / "custom_components" / "hermes_voice"


def load(module: str):
    """Return ``hermes_voice.<module>`` (relative imports inside it work)."""
    if _NAME not in sys.modules:
        pkg = types.ModuleType(_NAME)
        pkg.__path__ = [str(_DIR)]
        sys.modules[_NAME] = pkg
    return importlib.import_module(f"{_NAME}.{module}")


def aio(test):
    """Run an async test with asyncio.run (CI installs plain pytest, no asyncio plugin)."""

    @functools.wraps(test)
    def wrapper():
        asyncio.run(test())

    return wrapper
