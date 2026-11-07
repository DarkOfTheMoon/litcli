"""``search``: one backend or all of them, with aggregation and post-filters."""
from __future__ import annotations

import sys

from ..aggregate import cited_key, merge_items
from ..config import get_all_backends, get_backend
from ..errors import LitcliError
from ..models import AcademicItem
from .render import print_items


def search_filters(args) -> dict:
    """Collect the CLI search ``--title/--author/--year/--container`` into a filters dict (non-empty only)."""
    return {k: v for k, v in {
        "title": getattr(args, "title", None),
        "author": getattr(args, "author", None),
        "year": getattr(args, "year", None),
        "container": getattr(args, "container", None),
    }.items() if v}


def cmd_search(cfg, args) -> int:
    """Search one backend, or (the default) every enabled one plus the keyless OA corpora.

    Aggregating is the default because a caller should not have to know that the interesting
    results live outside their local store. A backend that fails is skipped with one stderr line
    instead of sinking the whole search; an explicitly requested ``--backend`` still raises.
    """
    filters = search_filters(args)
    if args.backend is None:
        # Corpora are keyless open-access sources: consulted even when unconfigured, mirroring
        # what ``fetch`` does for fulltext, so an empty config still searches the OA literature.
        backends = get_all_backends(cfg, corpora=True)
        items: list[AcademicItem] = []
        for name, b in backends.items():
            try:
                items.extend(b.search(args.query, limit=args.limit, filters=filters))
            except LitcliError as e:
                # Policy by kind, never by message: an unsupported operation is this backend's
                # normal state and stays silent; anything else gets one line, with the hint when
                # the failure has a fix the caller can act on.
                if e.kind != "unsupported":
                    print(f"[skipped] backend {name}: {e}"
                          + (f" ({e.hint})" if e.hint else ""), file=sys.stderr)
                continue
            except (NotImplementedError, RuntimeError, OSError, ValueError, SyntaxError) as e:
                # A single backend failure (not connected / timeout / non-JSON body / malformed
                # XML) must not bring down the whole aggregation; skip and continue.
                print(f"[skipped] backend {name}: {e}", file=sys.stderr)
                continue
    else:
        backend = get_backend(args.backend, cfg)
        items = backend.search(args.query, limit=args.limit, filters=filters)

    # Aggregation: dedup / merge / sort and mark sources only when several items came back.
    if args.backend is None and items and len(items) > 1:
        items = merge_items(items)

    # --type filter: drop noisy items (book titles / off-topic). Post-merge so --all dedup
    # is preserved; kept as a CLI post-filter for cross-backend semantic consistency.
    if getattr(args, "type", None):
        want = args.type.lower()
        items = [it for it in items if it.item_type.lower() == want]

    # --sort cited: descending by citation count (default is merge's year/title order).
    if getattr(args, "sort", None) == "cited":
        items = sorted(items, key=cited_key)

    # --limit is a cap on what the caller sees, not per backend: nine backends at 10 each would
    # otherwise return ninety rows for a ten-row request.
    items = items[:args.limit]

    return print_items(items, args.format, args.no_cite, "[no results]")
