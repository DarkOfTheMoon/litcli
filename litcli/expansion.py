"""One hop outward from a work: its foundations, its forward citations, its semantic neighbours.

The three axes are walked separately because they are not interchangeable: ``refs`` goes
backward for foundations, ``citing`` forward for the frontier, ``related`` sideways for
neighbours that share neither direction. Each has its own envelope (from the effort level),
and the envelope is also the per-hop upper bound on what the axis may still read — so a
backend that only implements one axis, or an axis the project has used up, costs exactly that
axis and nothing else.
"""
from __future__ import annotations

from collections.abc import Iterator

from .backends.base import NotSupportedError
from .models import AcademicItem

#: The axes, in walk order: foundations, forward frontier, neighbours.
AXES = ("refs", "citing", "related")


def expand(item: AcademicItem, backend, caps: dict, counters: dict) -> Iterator[tuple[AcademicItem, str]]:
    """Yield ``(candidate, axis)`` for one hop, charging each candidate to its axis.

    ``caps`` is the per-step ask, ``counters`` the remaining per-survey envelope; the smaller
    of the two is the real ask. Only candidates actually handed out are charged, so a short
    answer does not burn the envelope.
    """
    for axis in AXES:
        room = min(int(caps.get(axis, 0)), int(counters.get(axis, 0)))
        if room <= 0:
            continue
        call = {"refs": backend.references, "citing": backend.citing,
                "related": backend.related}[axis]
        try:
            found = call(item.key, limit=room)
        except NotSupportedError:
            continue
        for candidate in list(found)[:room]:
            counters[axis] = int(counters.get(axis, 0)) - 1
            yield candidate, axis
