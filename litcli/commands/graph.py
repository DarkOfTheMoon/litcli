"""Citation-graph edges (``refs`` / ``citing`` / ``related``)."""
from __future__ import annotations

import re
import sys

from ..aggregate import identity
from ..backends.base import NotSupportedError
from ..errors import LitcliError, Unavailable
from ..expansion import _titled
from ..provenance import stamp
from .keys import EDGE_METHODS, GRAPH_PREFERENCE, graph_sources, normalize_related_key, resolve_backend, split_source
from .render import print_items

#: Subcommand -> backend method (the table lives in ``keys`` beside the preference order).
__all__ = ["EDGE_METHODS", "cmd_edge"]

#: Words that carry nothing into a keyword search.
_STOP = {"a", "an", "the", "of", "on", "in", "for", "and", "to", "with", "by", "from", "at", "as"}

#: Axes where the neighbourhood itself is the point, so every source that answers adds to it.
_UNION = frozenset({"references", "related"})


def _keywords(title: str, count: int = 5) -> list[str]:
    words = [w for w in re.findall(r"\w+", (title or "").lower()) if w not in _STOP]
    return sorted(set(words), key=lambda w: (-len(w), words.index(w)))[:count]


def _keyword_related(backend, key: str, limit: int):
    """Fallback when the graph lists no neighbours: search the seed's own title terms.

    Many older or obscure works simply have no ``related_works``; an empty answer with no
    alternative reads as "nothing close exists", which is usually wrong.
    """
    seed = backend.get(key)
    terms = _keywords(seed.title)
    if not terms:
        return []
    found = backend.search(" ".join(terms), limit=max(limit * 3, 10))
    mine = identity(seed)
    out = [it for it in found if identity(it) != mine]
    for it in out:
        it.extra["related_via"] = "keyword"
    return out[:limit]


def _sources(cfg, args) -> list[tuple[object, str]]:
    """``(backend, key)`` pairs to try: a pinned one, or the graph sources in preference order.

    ``--backend`` and a ``source/`` prefix both pin: the reader said which source they mean.
    """
    if args.backend:
        return [resolve_backend(cfg, args.backend, args.key, fallback=GRAPH_PREFERENCE[0])]
    if split_source(args.key)[0]:
        return [resolve_backend(cfg, None, args.key, fallback=GRAPH_PREFERENCE[0])]
    return [(source, args.key) for source in graph_sources(cfg)]


def _cited_key(item) -> tuple[int, int]:
    """Sort key for a merged table: cited rows first, best cited first, uncited ones last."""
    count = int(getattr(item, "cited_by_count", 0) or 0)
    return (0, -count) if count else (1, 0)


def cmd_edge(cfg, args, method: str) -> int:
    """Dispatch ``method`` (references/citing/related), falling over to the next source.

    ``refs`` and ``related`` are a neighbourhood hunt, so the sources after the first one keep
    adding to the table until it is full; ``citing`` is left to a single index because the
    citation indexes overlap heavily and a second one would buy mostly duplicates. Pinning a
    source (``--backend`` or a ``source/`` prefix) means the reader named it, so nothing is
    added from anywhere else.
    """
    items: list = []
    extra: list = []
    last: Exception | None = None
    #: The last wall (throttle / spent budget). It outranks a 404 as the reason to report: "the
    #: source is spent" explains an empty edge better than "that bare DOI service has no record".
    walled: Exception | None = None
    answered: tuple[object, str] | None = None
    pairs = _sources(cfg, args)
    union = method in _UNION and len(pairs) > 1
    for backend, key in pairs:
        bare = normalize_related_key(key)
        if union and len(items) >= args.limit:
            break  # the table is full; another source would only add rows that get truncated
        try:
            # Ordering (by citation count) is owned by the backend, selected via ``sort_cited``.
            found = stamp(getattr(backend, method)(bare, args.limit if not union
                                                   else max(1, args.limit - len(items)),
                                                   sort_cited=(args.sort == "cited")), backend)
        except NotSupportedError as e:
            last = last or e
            continue
        except Unavailable as e:
            # A throttled or spent source is exactly what the fallback is for: say it and go
            # on, because a 429 from the first source is not the answer.
            last = walled = e
            print(f"[skipped] {getattr(backend, 'name', backend)}: {e}", file=sys.stderr)
            continue
        except (LitcliError, RuntimeError, OSError) as e:
            # A 404 here is the service saying it has no such record; the next source may.
            last = e
            print(f"[skipped] {getattr(backend, 'name', backend)}: {e}", file=sys.stderr)
            continue
        if answered is None:
            # The preferred source sets the table's order; an empty answer still lets the next
            # source speak and still leaves the keyword fallback below reachable.
            answered = (backend, bare)
            items = found
        elif found:
            extra.extend(found)
        if items and not union:
            break
    if answered is None:
        if walled is not None or last is not None:
            raise walled or last
        return print_items(items, args.format, args.no_cite, "[no related]")
    if extra:
        # An addition is a candidate like any other: same work under one identity, and a bare
        # DOI from a link source is hydrated below together with the base rows.
        have = {identity(it) for it in items}
        pool = []
        for it in extra:
            mine = identity(it)
            if mine not in have:
                have.add(mine)
                pool.append(it)
        # Cited order sorts the whole table; otherwise the first source's order is the reader's
        # expectation and the extras sort only among themselves.
        items = (sorted(items + pool, key=_cited_key) if args.sort == "cited"
                 else items + sorted(pool, key=_cited_key))
        items = items[: args.limit]
    if any(not (it.title or "").strip() for it in items):
        # A link-only source (COCI) hands back bare DOIs; a row without a title is not usable,
        # so it is filled in from whichever source can read it. A pinned source is not a reason
        # to show bare rows, so the rest of the graph sources are a hydration pool too. Best
        # effort, never fatal.
        pool = [b for b, _k in pairs] + [b for b in graph_sources(cfg) if b not in
                                         [p for p, _k in pairs]]
        items = [it if (it.title or "").strip() else _titled(it, pool) for it in items]
    if method == "related" and not items and hasattr(answered[0], "search"):
        # Stamp it like a graph hit, or the [oa]/[public] provenance labels vanish on the fallback.
        items = stamp(_keyword_related(answered[0], answered[1], args.limit), answered[0])
        if items:
            print("[related] no graph neighbours listed; matching on the title instead",
                  file=sys.stderr)
    return print_items(items, args.format, args.no_cite, "[no related]")
