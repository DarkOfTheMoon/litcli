"""Citation-graph edges (``refs`` / ``citing`` / ``related``)."""
from __future__ import annotations

from ..provenance import stamp
from .keys import normalize_related_key, resolve_backend
from .render import print_items

#: Subcommand -> backend method.
EDGE_METHODS = {"refs": "references", "citing": "citing", "related": "related"}


def cmd_edge(cfg, args, method: str) -> int:
    """Dispatch ``method`` (references/citing/related) on one backend."""
    backend, key = resolve_backend(cfg, args.backend, args.key, fallback="openalex")
    # Only openalex implements the graph edges; the rest raise NotSupportedError (caught in main).
    # Ordering (by citation count) is owned by the backend, selected here via ``sort_cited``.
    items = stamp(getattr(backend, method)(normalize_related_key(key), args.limit,
                                           sort_cited=(args.sort == "cited")), backend)
    return print_items(items, args.format, args.no_cite, "[no related]")
