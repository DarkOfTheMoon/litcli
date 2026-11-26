"""Citation-graph edges (``refs`` / ``citing`` / ``related``)."""
from __future__ import annotations

import re
import sys

from ..aggregate import identity
from ..backends.base import NotSupportedError
from ..errors import BadInput, LitcliError, Unavailable
from ..expansion import _titled, target_for
from ..models import AcademicItem
from ..provenance import stamp
from .keys import EDGE_METHODS, GRAPH_PREFERENCE, graph_sources, normalize_related_key, resolve_backend, split_source
from .render import print_items

#: Subcommand -> backend method (the table lives in ``keys`` beside the preference order).
__all__ = ["EDGE_METHODS", "cmd_edge"]

#: Words that carry nothing into a keyword search.
_STOP = {"a", "an", "the", "of", "on", "in", "for", "and", "to", "with", "by", "from", "at", "as"}

#: Axes where the neighbourhood itself is the point, so every source that answers adds to it.
_UNION = frozenset({"references", "related"})

#: What a ``--limit`` of zero or less asks the sources for: no cap, as on every other command.
_NO_CAP = 200


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


def _hydration_pool(pairs, cfg) -> list:
    """Sources a bare row may be filled from: those in hand first, then the rest, each once.

    ``graph_sources`` builds fresh instances, so a source already in ``pairs`` never compares
    equal by identity to its own twin; the pool is deduped by name or the same lookup is made
    twice.
    """
    out: list = []
    seen: set[str] = set()
    for backend in [b for b, _k in pairs] + graph_sources(cfg):
        name = str(getattr(backend, "name", "") or id(backend))
        if name in seen:
            continue
        seen.add(name)
        out.append(backend)
    return out


def _target(source, key: str) -> str | None:
    """The spelling ``source`` takes for ``key``, or ``None`` when it has none.

    A W-id means nothing to the DOI-pair services and a bare DOI means nothing to a source
    that also carries work ids; asking anyway costs a request and a ``[skipped]`` line for a
    404 that was certain.
    """
    doi = key if key.startswith("10.") else ""
    return target_for(source, AcademicItem(key=key, doi=doi))


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
    #: 0 and negatives mean no cap, the same as on ``search``; the sources are asked for
    #: everything and the table is not truncated.
    limit = args.limit if args.limit > 0 else _NO_CAP
    items: list = []
    extra: list = []
    last: Exception | None = None
    #: The last wall (throttle / spent budget). It outranks a 404 as the reason to report: "the
    #: source is spent" explains an empty edge better than "that bare DOI service has no record".
    walled: Exception | None = None
    answered: tuple[object, str] | None = None
    try:
        pairs = _sources(cfg, args)
    except (KeyError, RuntimeError) as e:  # an unknown or disabled `--backend` / prefix
        raise BadInput(str(e)) from e
    union = method in _UNION and len(pairs) > 1
    for backend, key in pairs:
        bare = normalize_related_key(key)
        target = _target(backend, bare)
        if target is None:
            continue  # this source has no spelling for the key; it could only answer 404
        if union and len(items) >= limit:
            break  # the table is full; another source would only add rows that get truncated
        try:
            # Ordering (by citation count) is owned by the backend, selected via ``sort_cited``.
            found = stamp(getattr(backend, method)(target, limit if not union
                                                   else max(1, limit - len(items)),
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
        except (LitcliError, RuntimeError, OSError, KeyError, AttributeError, ValueError,
                TypeError) as e:
            # A 404 here is the service saying it has no such record; the next source may. A
            # bare KeyError is a 200 with an empty body and an AttributeError a 200 that is
            # shaped differently than expected — one unreadable answer, not the end.
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
    # An addition is a candidate like any other: the same work under one identity is kept once.
    base_rows = len(items)
    if extra:
        have = {identity(it) for it in items}
        for it in extra:
            mine = identity(it)
            if mine not in have:
                have.add(mine)
                items.append(it)
    if any(not (it.title or "").strip() for it in items):
        # A link-only source (COCI) hands back bare DOIs; a row without a title is not usable,
        # so it is filled in from whichever source can read it. A pinned source is not a reason
        # to show bare rows, so the rest of the graph sources are a hydration pool too. Best
        # effort, never fatal. Runs before the sort: the fill brings the citation counts the
        # order is made of.
        items = [it if (it.title or "").strip() else _titled(it, _hydration_pool(pairs, cfg))
                 for it in items]
    if args.sort == "cited":
        # The whole table, whichever source each row came from — and after the fill, or every
        # row from a link source would sort as zero.
        items.sort(key=_cited_key)
    elif extra:
        # Otherwise the first source's order is the reader's expectation and the additions sort
        # only among themselves; also after the fill, for the same reason.
        items[base_rows:] = sorted(items[base_rows:], key=_cited_key)
    if args.limit > 0:
        items = items[: args.limit]
    if method == "related" and not items and hasattr(answered[0], "search"):
        # Stamp it like a graph hit, or the [oa]/[public] provenance labels vanish on the fallback.
        items = stamp(_keyword_related(answered[0], answered[1], limit), answered[0])
        if items:
            print("[related] no graph neighbours listed; matching on the title instead",
                  file=sys.stderr)
    return print_items(items, args.format, args.no_cite, "[no related]")
