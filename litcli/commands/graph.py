"""Citation-graph edges (``refs`` / ``citing`` / ``related``)."""
from __future__ import annotations

import re
import sys

from ..aggregate import identity
from ..provenance import stamp
from .keys import normalize_related_key, resolve_backend
from .render import print_items

#: Subcommand -> backend method.
EDGE_METHODS = {"refs": "references", "citing": "citing", "related": "related"}

#: Words that carry nothing into a keyword search.
_STOP = {"a", "an", "the", "of", "on", "in", "for", "and", "to", "with", "by", "from", "at", "as"}


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


def cmd_edge(cfg, args, method: str) -> int:
    """Dispatch ``method`` (references/citing/related) on one backend."""
    backend, key = resolve_backend(cfg, args.backend, args.key, fallback="openalex")
    # Only openalex implements the graph edges; the rest raise NotSupportedError (caught in main).
    # Ordering (by citation count) is owned by the backend, selected here via ``sort_cited``.
    bare = normalize_related_key(key)
    items = stamp(getattr(backend, method)(bare, args.limit, sort_cited=(args.sort == "cited")),
                  backend)
    if method == "related" and not items and hasattr(backend, "search"):
        items = _keyword_related(backend, bare, args.limit)
        if items:
            print("[related] no graph neighbours listed; matching on the title instead",
                  file=sys.stderr)
    return print_items(items, args.format, args.no_cite, "[no related]")
