"""Citation-graph edges (``refs`` / ``citing`` / ``related``)."""
from __future__ import annotations

from ..config import get_backend
from .keys import normalize_related_key
from .render import print_items

#: Subcommand -> backend method.
EDGE_METHODS = {"refs": "references", "citing": "citing", "related": "related"}


def cmd_edge(cfg, args, method: str) -> int:
    """Dispatch ``method`` (references/citing/related) on one backend."""
    backend = get_backend(args.backend, cfg)
    # Only openalex implements the graph edges; the rest raise NotSupportedError (caught in main).
    # Ordering (by citation count) is owned by the backend, selected here via ``sort_cited``.
    items = getattr(backend, method)(normalize_related_key(args.key), args.limit,
                                     sort_cited=(args.sort == "cited"))
    return print_items(items, args.format, args.no_cite, "[no related]")
