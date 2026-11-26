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

#: Words that stand in nearly every title, so matching one says nothing about the topic. Left
#: in, they hand any record a share of every question that happens to use them.
_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "been", "by", "for", "from", "in", "into", "is",
    "it", "its", "of", "on", "or", "such", "than", "that", "the", "their", "them", "then",
    "there", "these", "they", "this", "those", "to", "upon", "via", "was", "were", "what",
    "when", "where", "which", "while", "who", "will", "with", "without",
})

#: Words that join the parts of a question ("X for Y", "A in B"). They are stopwords already;
#: here they also mark where one part ends and the next begins, so the parts can be required one
#: by one instead of the whole string being averaged.
_LEG_JOINERS = frozenset({"for", "in", "of", "on", "onto", "using", "via", "with", "without",
                          "to", "from", "into", "upon", "by", "under", "through", "and"})

#: Suffix rewrites of the smallest stemmer that is still worth having — Porter's step 1 minus
#: the parts that need the full algorithm. Titles and questions disagree on number constantly,
#: and a literal comparison throws away the half of the question that happens to be plural.
_SUFFIXES = (("ies", "y"), ("es", ""), ("s", ""), ("ing", ""), ("ed", ""))

#: Share of a long question's parts that must be carried to count as an answer. Elasticsearch's
#: ``minimum_should_match`` conditional form (``2<75%``): every part of a one- or two-part
#: question ("X for Y" has both halves), 75% of a longer one (rounded down, never under one).
#: Three parts is where the questions in use sit ("X of Y in Z"), and demanding all three starves
#: the walk: the literature answers two of them at a time, never all three in one title.
LEG_SHARE = 0.75

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
    return [t for t in (w.lower() for w in _TOKEN_RE.findall(query or ""))
            if t not in _STOPWORDS]


def _stem(word: str) -> str:
    for suffix, replacement in _SUFFIXES:
        if len(word) > len(suffix) + 2 and word.endswith(suffix):
            return word[: -len(suffix)] + replacement
    return word


def _same(a: str, b: str) -> bool:
    """Whether two words are the same word: equal stems, or one inside the other.

    The containment covers compounding — ``nanofluidic`` carries ``fluidic``, and
    ``viscoelastic`` carries the ``elastic`` a title may spell on its own. It needs four
    characters at the shorter end, or the article ``a`` sits inside half the query.
    """
    if _stem(a) == _stem(b):
        return True
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    return len(short) >= 4 and short in long_


def legs(query: str) -> list[list[str]]:
    """The question's parts, split at the words that join them ("X for Y", "A in B").

    A question is usually a conjunction: the work that answers only one half of "machine
    learning for thermal hydraulics" is not the work that was asked for. Splitting keeps the
    parts separately weighable, so one of them cannot be bought with the other's words.
    """
    parts: list[list[str]] = [[]]
    for word in _TOKEN_RE.findall(query or ""):
        word = word.lower()
        if word in _LEG_JOINERS:
            parts.append([])
        elif word not in _STOPWORDS:
            parts[-1].append(word)
    return [part for part in parts if part]


def _required(count: int) -> int:
    """How many parts must be carried — all of two or fewer, :data:`LEG_SHARE` of more, min one."""
    if count <= 2:
        return count
    return max(1, int(count * LEG_SHARE))


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


def _leg_coverage(item: AcademicItem, part: list[str]) -> float:
    """Share of one part's words this item carries, in ``0..1``.

    A title hit counts whole, an abstract hit for half: a title is what the authors chose the
    work to be about. Matching is by substring, so a query word inside a longer one still
    counts (``wick`` in ``wicking``) — cheap, and close enough for ranking.
    """
    title = _TOKEN_RE.findall((item.title or "").lower())
    abstract = _TOKEN_RE.findall((item.abstract or "").lower())
    hits = sum(1 for w in part if any(_same(w, t) for t in title))
    hits += 0.5 * sum(1 for w in part if any(_same(w, t) for t in abstract))
    return min(1.0, hits / max(2, len(part)))


def coverage(item: AcademicItem, query: str) -> float:
    """Share of the query's content words the item carries, squared — the ranking axis ``q``.

    Unlike :func:`overlap` this knows nothing about the question's parts: partial answers keep
    their proportion, which is what orders two candidates that both already passed the gate. The
    share is divided by two words at least, so a one-word question cannot be answered whole by a
    single hit, and squared, so carrying half of what was asked is not half an answer.
    """
    tokens = _tokens(query)
    if not tokens:
        return 0.0
    return _leg_coverage(item, tokens) ** 2


def overlap(item: AcademicItem, query: str) -> float:
    """How much of the question the item answers, in ``0..1``, as the *weakest required part*.

    The parts are the question's conjunction members (:func:`legs`), and answering the question
    means carrying enough of them — all of a one- or two-part question, three quarters of a
    longer one (see :data:`LEG_SHARE`). The score is the smallest coverage among the parts it
    had to carry, so one strong half cannot buy a missing one; a part matching nothing drags the
    result to 0 however many of the other words are there.

    Squared, because carrying half of a part is not half an answer: a generic work matching one
    word of a seven-word question would otherwise count as on-topic.
    """
    parts = legs(query)
    if not parts:
        return 0.0
    coverages = sorted(_leg_coverage(item, part) for part in parts)
    required = coverages[-_required(len(parts)):]
    return required[0] ** 2


def relevance(item: AcademicItem, query: str) -> tuple:
    """A descending sort key for "how well does this answer the query".

    Title matches outweigh the abstract, and a hit containing every token beats one that only
    shares a word; citation count breaks ties, then year and title make the order stable.
    """
    tokens = _tokens(query)
    title = _TOKEN_RE.findall((item.title or "").lower())
    abstract = _TOKEN_RE.findall((item.abstract or "").lower())
    container = _TOKEN_RE.findall((item.container_title or "").lower())
    score = sum(3 for t in tokens if any(_same(t, w) for w in title))
    score += sum(2 for t in tokens if any(_same(t, w) for w in container))
    score += sum(1 for t in tokens if any(_same(t, w) for w in abstract))
    if tokens and all(any(_same(t, w) for w in title) for t in tokens):
        score += 5
    return (-score, -item.cited_by_count, -(int(item.year) if (item.year or "").isdigit() else 0),
            (item.title or ""))


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
