"""Typed backend failures, so fall-back policy is not string matching.

A backend raises the kind of failure it hit; the caller decides what that means:

===========  ==========================================================
kind         the caller should...
===========  ==========================================================
bad-input    report it; retrying the same target is pointless
not-found    move on — this source simply has no record / no copy
paywalled    try another source, the work exists but is gated
auth         ask for credentials or a one-off authorization
bot-wall     try another source; only the site's own flow gets past it
unavailable  try another source, or retry later
unsupported  skip it (the backend cannot do this operation at all)
external     report the bundled tool / subprocess failure
===========  ==========================================================

All of them derive from :class:`LitcliError`, which is what the CLI maps to an exit code;
the ``kind`` is what ``--all`` and the fulltext chain switch on.
"""
from __future__ import annotations

#: Exit code the CLI uses for any failure (1 is reserved for "no fulltext").
ERROR_EXIT = 2


class LitcliError(Exception):
    """Base for every expected failure; message is user-facing."""

    kind = "error"
    exit_code = ERROR_EXIT

    def __init__(self, message: str, hint: str = ""):
        self.hint = hint
        super().__init__(message)


class BadInput(LitcliError):
    """The target or option is malformed."""

    kind = "bad-input"


class NotFound(LitcliError):
    """This source has no such record, or no copy of the fulltext."""

    kind = "not-found"


class Paywalled(LitcliError):
    """The work exists but sits behind a subscription."""

    kind = "paywalled"


class AuthRequired(LitcliError):
    """Credentials, a key or a one-off authorization are missing or rejected."""

    kind = "auth"


class BotWall(LitcliError):
    """An anti-bot page (captcha, slider, JS challenge) stands in the way."""

    kind = "bot-wall"


class Unavailable(LitcliError):
    """Network failure, or a 5xx that survived the retries."""

    kind = "unavailable"


class Unsupported(LitcliError):
    """The backend does not implement the operation."""

    kind = "unsupported"

    def __init__(self, message: str):
        super().__init__(message)


class External(LitcliError):
    """A bundled tool or subprocess (browser driver, pdftotext) failed."""

    kind = "external"


def http_kind(status: int) -> type[LitcliError]:
    """The class for a non-retryable HTTP status.

    403 is read as a wall because the sources here use it that way (Cloudflare / WAF);
    backends that mean "bad key" or "quiet 200" reclassify at their own call site.
    """
    if status in (401,):
        return AuthRequired
    if status in (402, 451):
        return Paywalled
    if status in (403,):
        return BotWall
    if status in (404, 410):
        return NotFound
    if status in (400, 422):
        return BadInput
    return Unavailable


def for_status(status: int, message: str) -> LitcliError:
    """An instance of the class :func:`http_kind` picks, for backends that have a raw status."""
    return http_kind(status)(message)
