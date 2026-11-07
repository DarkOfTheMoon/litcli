"""Opt-in tracing to stderr, for when a command misbehaves and the one-line error is not enough.

Enabled by ``litcli --debug ...`` or ``LITCLI_DEBUG=1`` in the environment. Nothing here runs
unless enabled, so tracing costs a boolean check on the normal path.

The trace covers what the caller cannot see: the request line, its outcome and timing, the
pacing sleeps, and retry attempts. Secrets carried in query strings (API keys, the polite-pool
e-mail) are masked before they reach the terminal.
"""
from __future__ import annotations

import os
import re
import sys
import time

#: Truthy values of ``LITCLI_DEBUG``.
_TRUTHY = {"1", "true", "yes", "on"}
_SECRET = re.compile(r"(\b(?:api_key|apikey|key|token|email)=)[^&]+", re.I)

_ENABLED = os.environ.get("LITCLI_DEBUG", "").lower() in _TRUTHY


def enabled() -> bool:
    return _ENABLED


def set_enabled(flag: bool = True) -> None:
    """Turn tracing on (``--debug``); the environment can only turn it on, never off."""
    global _ENABLED
    if flag:
        _ENABLED = True


def mask(url: str) -> str:
    """Hide query values that are credentials or a personal address."""
    return _SECRET.sub(r"\1***", url)


def log(message: str) -> None:
    """One trace line; stderr so ``--format json`` on stdout stays parseable."""
    if _ENABLED:
        print(f"[debug] {message}", file=sys.stderr, flush=True)


def prefix(text: str) -> str:
    """First line of a failure, for retry lines that should not dump a whole body."""
    return text.splitlines()[0] if text else ""


class Timer:
    """Elapsed seconds for one request, tolerant of transports that do not set ``elapsed``."""

    def __init__(self):
        self._start = time.monotonic()

    @property
    def seconds(self) -> float:
        return time.monotonic() - self._start


def hooks() -> dict:
    """``httpx`` event hooks that trace every request and response.

    Returns an empty mapping when tracing is off, so callers can pass it unconditionally.
    """
    if not _ENABLED:
        return {}

    def on_request(request) -> None:
        request.extensions["litcli_timer"] = Timer()
        log(f"--> {request.method} {mask(str(request.url))}")

    def on_response(response) -> None:
        timer = response.request.extensions.get("litcli_timer")
        took = timer.seconds if timer else float("nan")
        size = response.headers.get("content-length", "?")
        log(f"<-- {response.status_code} {mask(str(response.url))} "
            f"({took:.3f}s, {size} bytes)")

    return {"request": [on_request], "response": [on_response]}
