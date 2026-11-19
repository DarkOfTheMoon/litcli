"""``search``: one backend or all of them, with aggregation and post-filters."""
from __future__ import annotations

import sys

from ..aggregate import cited_key, merge_items
from ..config import get_all_backends, get_backend
from ..debug import notice
from ..errors import LitcliError
from ..models import AcademicItem
from ..provenance import stamp
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
    results live outside their local store. Fan-out reads answer "what exists across sources",
    so a failing source is silent by default and only ``--verbose`` names it; a search where
    every source failed is not an empty result, though, and says so with exit 2. An explicitly
    requested ``--backend`` always raises.
    """
    filters = search_filters(args)
    if args.backend is None:
        # Corpora are keyless open-access sources: consulted even when unconfigured, mirroring
        # what ``fetch`` does for fulltext, so an empty config still searches the OA literature.
        backends = get_all_backends(cfg, corpora=True)
        items: list[AcademicItem] = []
        failed: dict[str, int] = {}
        for name, b in backends.items():
            try:
                items.extend(stamp(b.search(args.query, limit=args.limit, filters=filters), b))
            except LitcliError as e:
                # Policy by kind, never by message: an unsupported operation is this backend's
                # normal state and counts for nothing; anything else is a failure to name.
                if e.kind != "unsupported":
                    failed[e.kind] = failed.get(e.kind, 0) + 1
                    notice(f"[skipped] backend {name}: {e}"
                           + (f" ({e.hint})" if e.hint else ""))
                continue
            except (NotImplementedError, RuntimeError, OSError, ValueError, SyntaxError) as e:
                # A single backend failure (timeout / non-JSON body / malformed XML) must not
                # bring down the whole aggregation; count it and continue.
                failed[type(e).__name__] = failed.get(type(e).__name__, 0) + 1
                notice(f"[skipped] backend {name}: {e}")
                continue
        if failed:
            summary = ", ".join(f"{kind}:{count}" for kind, count in sorted(failed.items()))
            notice(f"[partial] {sum(failed.values())}/{len(backends)} sources failed ({summary})")
            if not items and sum(failed.values()) == len(backends):
                # Not "nothing exists" — nobody answered. Saying nothing here would read as a
                # complete empty result, so this one case is loud even without --verbose (to
                # stderr under a machine format, where stdout must stay parseable).
                line = f"[no results] all {len(backends)} sources failed; --verbose for why"
                machine = args.format in ("json", "csl")
                print(line, file=sys.stderr if machine else sys.stdout)
                if machine:
                    print_items([], args.format, args.no_cite, line)
                return 2
    else:
        backend = get_backend(args.backend, cfg)
        items = stamp(backend.search(args.query, limit=args.limit, filters=filters), backend)

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
