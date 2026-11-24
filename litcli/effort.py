"""How wide a survey may walk, as a named level rather than a budget.

How much material a question is worth is a property of the topic, not a number the user can
predict in advance ("a hot field fills up fast, a cold one never will"), so the user picks a
level and each level is an envelope: how many candidates enter the frontier per step, how many
are admitted, how many edges each direction is read from, how long a dry streak may run before
the walk stops, and how many requests every site may see.

A row is a plain dict so a caller can override one cell; :func:`resolve` hands out a copy and
never the module table.
"""
from __future__ import annotations

from .errors import BadInput

LEVELS = ("low", "medium", "high", "max")
DEFAULT = "medium"

#: The envelope per level: ``frontier`` candidates per step, ``admit`` of them kept, ``refs`` /
#: ``citing`` / ``related`` edges read from each work, ``streak`` dry steps allowed,
#: ``openalex_cap`` / ``arxiv_cap`` / ``publisher_cap`` requests per site, ``max_hops`` steps.
_TABLE = {
    "low": dict(frontier=4, admit=2, refs=10, citing=10, related=5, streak=2,
                openalex_cap=40, arxiv_cap=10, publisher_cap=2, max_hops=6),
    "medium": dict(frontier=8, admit=5, refs=20, citing=20, related=10, streak=3,
                   openalex_cap=200, arxiv_cap=40, publisher_cap=6, max_hops=20),
    "high": dict(frontier=16, admit=8, refs=30, citing=30, related=15, streak=4,
                 openalex_cap=600, arxiv_cap=100, publisher_cap=15, max_hops=50),
    "max": dict(frontier=24, admit=12, refs=50, citing=50, related=25, streak=6,
                openalex_cap=1500, arxiv_cap=200, publisher_cap=30, max_hops=120),
}


def resolve(level: str, overrides: dict | None = None) -> dict:
    """The envelope for ``level``, with ``overrides`` applied on top."""
    if level not in _TABLE:
        raise BadInput(f"unknown effort level {level!r}; choose one of: {', '.join(LEVELS)}")
    row = dict(_TABLE[level])
    row.update(overrides or {})
    return row
