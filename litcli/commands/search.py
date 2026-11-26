"""``search``: one backend or all of them, with aggregation and post-filters."""
from __future__ import annotations

import re
import sys
import threading
import time

from ..aggregate import cited_key, merge_items
from ..config import get_all_backends, get_backend
from ..debug import notice
from ..errors import LitcliError
from ..models import AcademicItem
from ..provenance import stamp
from .render import print_items

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)

#: Seconds the whole fan-out may take before the stragglers are given up on.
DEADLINE = 45.0
#: Sources searched at the same time.
MAX_WORKERS = 4


def _fan_out(backends: dict, query: str, limit: int, filters: dict,
             deadline: float | None = None) -> dict[str, tuple]:
    """Search ``backends`` in parallel and return ``{name: (items, exc)}`` for those that answered.

    The workers are *daemon* threads: once ``deadline`` passes the caller walks away and a stuck
    request cannot hold the process (``ThreadPoolExecutor`` threads are joined at interpreter
    exit, which turned the 45-second cap into a 90-second one). A semaphore keeps at most
    ``MAX_WORKERS`` requests in flight, and a name missing from the result was still running.
    """
    deadline = DEADLINE if deadline is None else deadline
    results: dict[str, tuple] = {}
    slots = threading.Semaphore(MAX_WORKERS)

    def work(name: str) -> None:
        with slots:
            backend = backends[name]
            try:
                results[name] = (stamp(backend.search(query, limit=limit, filters=filters), backend),
                                 None)
            except Exception as exc:  # classified by the caller, whatever the type
                results[name] = ([], exc)

    threads = [threading.Thread(target=work, args=(name,), name=f"litcli-{name}", daemon=True)
               for name in backends]
    started = time.monotonic()
    for thread in threads:
        thread.start()
    for thread in threads:
        left = deadline - (time.monotonic() - started)
        if left <= 0:
            break
        thread.join(left)
    return results


def _tokens(query: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(query or "")]


def matches(item: AcademicItem, filters: dict) -> bool:
    """Enforce a filter locally as well, so a backend that ignores part of the mapping (or
    folds it into free text) cannot leak records past it."""
    def has(hay: str, needle: str) -> bool:
        return needle.lower() in (hay or "").lower()

    if not has(item.title, filters.get("title", "")):
        return False
    if not has(item.container_title, filters.get("container", "")):
        return False
    if filters.get("year") and not (item.year or "").startswith(filters["year"]):
        return False
    if filters.get("author"):
        names = [f"{c.get('given', '')} {c.get('family', '')}" for c in item.creators]
        if not any(has(n, filters["author"]) for n in names):
            return False
    return True


def overlap(item: AcademicItem, query: str) -> float:
    """Share of the query's words this item carries, in ``0..1``, saturating at all of them.

    A title hit counts whole, an abstract hit for half: a title is what the authors chose the
    work to be about. Matching is by substring, so a query word inside a longer one still
    counts (``for`` in ``formaldehyde``) — cheap, and close enough for ranking.
    """
    tokens = _tokens(query)
    if not tokens:
        return 0.0
    title = (item.title or "").lower()
    abstract = (item.abstract or "").lower()
    hits = sum(1 for t in tokens if t in title) + 0.5 * sum(1 for t in tokens if t in abstract)
    return min(1.0, hits / len(tokens))


def relevance(item: AcademicItem, query: str) -> tuple:
    """A descending sort key for "how well does this answer the query".

    Title matches outweigh the abstract, and a hit containing every token beats one that only
    shares a word; citation count breaks ties, then year and title make the order stable.
    """
    tokens = _tokens(query)
    title = (item.title or "").lower()
    abstract = (item.abstract or "").lower()
    container = (item.container_title or "").lower()
    score = sum(3 for t in tokens if t in title)
    score += sum(2 for t in tokens if t in container)
    score += sum(1 for t in tokens if t in abstract)
    if tokens and all(t in title for t in tokens):
        score += 5
    return (-score, -item.cited_by_count, -(int(item.year) if (item.year or "").isdigit() else 0),
            title)


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
        # A few sources at a time: sequential fan-out across the whole registry is slow and walks
        # straight into the rate limits the retries then have to back off from.
        names = list(backends)
        done = _fan_out(backends, args.query, args.limit, filters)
        for name in names:
            if name not in done:
                # A source that never answers must not hold the whole search hostage, which is
                # how one slow 429 cascade used to turn into a 120-second command.
                done[name] = ([], TimeoutError(f"no answer within {DEADLINE:.0f}s"))

        for name in names:
            found, exc = done[name]
            items.extend(found)
            if exc is None:
                continue
            if isinstance(exc, LitcliError):
                e = exc
                # Policy by kind, never by message: an unsupported operation is this backend's
                # normal state and counts for nothing; anything else is a failure to name.
                if e.kind != "unsupported":
                    failed[e.kind] = failed.get(e.kind, 0) + 1
                    notice(f"[skipped] backend {name}: {e}"
                           + (f" ({e.hint})" if e.hint else ""))
            else:
                # Any other failure (playwright's TimeoutError, a KeyError from a parser, ...) is
                # still just one source failing: count it, keep the results the others gave.
                failed[type(exc).__name__] = failed.get(type(exc).__name__, 0) + 1
                notice(f"[skipped] backend {name}: {type(exc).__name__}: {exc}")
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

    # Backends map filters where they can; enforce them here too, because "the API said so" is
    # not a guarantee and a filter that only reaches half the sources is worse than none.
    if filters:
        items = [it for it in items if matches(it, filters)]

    # --type filter: drop noisy items (book titles / off-topic). Post-merge so --all dedup
    # is preserved; kept as a CLI post-filter for cross-backend semantic consistency.
    if getattr(args, "type", None):
        want = args.type.lower()
        items = [it for it in items if it.item_type.lower() == want]

    # Default order: relevance to the query (title beats abstract, more tokens beat fewer),
    # then citations. Without it ``--limit`` would return whichever backend answered first.
    # --sort cited overrides with a pure citation ranking.
    if getattr(args, "sort", None) == "cited":
        items = sorted(items, key=cited_key)
    else:
        items = sorted(items, key=lambda it: relevance(it, args.query))

    # --limit is a cap on what the caller sees, not per backend: nine backends at 10 each would
    # otherwise return ninety rows for a ten-row request.
    # A negative limit used to slice from the tail ("all but the last five"), which was nobody's
    # intent; 0 and negatives now mean "no cap".
    if args.limit and args.limit > 0:
        items = items[:args.limit]

    return print_items(items, args.format, args.no_cite, "[no results]", snippet=True)
