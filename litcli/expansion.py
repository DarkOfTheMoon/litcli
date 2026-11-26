"""One hop outward from a work: its foundations, its forward citations, its semantic neighbours.

The three axes are walked separately because they are not interchangeable: ``refs`` goes
backward for foundations, ``citing`` forward for the frontier, ``related`` sideways for
neighbours that share neither direction. Each has its own envelope (from the effort level),
and the envelope is also the per-hop upper bound on what the axis may still read — so a
backend that only implements one axis, or an axis the project has used up, costs exactly that
axis and nothing else.

An axis may be answerable by more than one source, and the sources do not all speak the same
identity (OpenAlex takes a work id or ``doi:``, Semantic Scholar ``DOI:``, the DOI-only services
the bare DOI). The list is asked in the caller's order until the axis room is spent; a work
already handed out by an earlier source is not handed out twice.
"""
from __future__ import annotations

import re
from collections.abc import Iterator, Sequence

from .aggregate import identity
from .backends.base import GRAPH_AXES, GraphSource, NotSupportedError
from .errors import LitcliError
from .models import AcademicItem

#: The axes, in walk order: foundations, forward frontier, neighbours.
AXES = ("refs", "citing", "related")

#: Sources that address a work by DOI only.
_DOI_ONLY = frozenset({"crossref", "opencitations"})

#: A Semantic Scholar paper id, and a key Europe PMC can take besides a DOI.
_S2_ID = re.compile(r"[0-9a-f]{40}")
_PMC_KEY = re.compile(r"(?:PMC)?\d+", re.I)


def target_for(source, item: AcademicItem) -> str | None:
    """The spelling ``source`` understands for ``item``, or ``None`` when it has none.

    A source that carries no DOI cannot be asked about a DOI-only item, and vice versa; the
    caller skips the pair instead of spending a request that can only 404.
    """
    name = str(getattr(source, "name", ""))
    doi = (item.doi or "").strip()
    if not doi:
        key = str(item.key or "")
        doi = key[len("doi:"):] if key.startswith("doi:") else (
            key if key.startswith("10.") else "")
    known = str((item.extra or {}).get("openalex_id") or "")
    if name == "openalex":
        key = str(item.key or "")
        if key.startswith("W") and key[1:].isdigit():
            return key
        if key.startswith("10.") or key.startswith("doi:"):
            return key
        if known:
            return known.rstrip("/").rsplit("/", 1)[-1]
        return f"doi:{doi}" if doi else None
    if name == "semanticscholar":
        if doi:
            return f"DOI:{doi}"
        # A DOI-less S2 row keeps its own 40-hex paper id; that is a spelling S2 accepts.
        key = str(item.key or "")
        return key if _S2_ID.fullmatch(key) else None
    if name in _DOI_ONLY and not doi:
        # Crossref and COCI address works by DOI alone; any other key can only 404.
        return None
    if name == "europepmc":
        # EPMC takes a DOI or a PMID / PMCID, never a title: a title-keyed row (a Crossref
        # reference without a DOI) would be a request that comes back empty.
        key = str(item.key or "")
        # Full match: a title that only starts with a year ("2020 survey of …") is not an id.
        return doi or (key if _PMC_KEY.fullmatch(key) else None)
    return doi or str(item.key or "") or None


def _titled(candidate: AcademicItem, sources: Sequence[GraphSource]) -> AcademicItem:
    """Fill in a bare candidate (a DOI from a link-only source) from a metadata source.

    Best effort: a failed hydration leaves the bare item, which still carries its identity.
    """
    if (candidate.title or "").strip():
        return candidate
    for source in sources:
        getter = getattr(source, "get", None)
        if getter is None or "read" not in getattr(source, "capabilities", set()):
            continue
        target = target_for(source, candidate)
        if not target:
            continue
        try:
            filled = getter(target)
        except Exception:  # a lookup is an improvement, never a requirement
            continue
        if filled is not None and (filled.title or "").strip():
            candidate.title = filled.title
            candidate.year = candidate.year or filled.year
            candidate.creators = candidate.creators or filled.creators
            candidate.container_title = candidate.container_title or filled.container_title
            candidate.item_type = filled.item_type or candidate.item_type
            candidate.cited_by_count = candidate.cited_by_count or filled.cited_by_count
            candidate.abstract = candidate.abstract or filled.abstract
            candidate.doi = candidate.doi or filled.doi
            return candidate
    return candidate


def _site(source) -> str:
    """The counter key a source's site budget is kept under."""
    return str(getattr(source, "name", "") or "graph")


def _spent(counters: dict, site: str) -> bool:
    """True when this site's own budget is used up (a caller tracking none has no such key)."""
    return site in counters and int(counters[site]) <= 0


def expand(item: AcademicItem, sources: Sequence[GraphSource], caps: dict,
           counters: dict, attempts: dict | None = None) -> Iterator[tuple[AcademicItem, str]]:
    """Yield ``(candidate, axis)`` for one hop, charging each candidate to its axis.

    ``sources`` is a backend or a list of them, tried in that order per axis; a source is only
    asked for an axis it declares in ``capabilities``. ``caps`` is the configured allowance for
    one work, ``counters`` what is left of it (the caller refills the axis cells per row). The
    smaller of the two is the real ask, shared out across the sources. Only candidates actually
    handed out are charged to the axis, so a short answer does not burn the allowance.

    Each source also has its own site budget under its own name in ``counters``: the ask is
    charged there as it is made (``attempts`` counts those per source, for the caller to
    reconcile against the requests really spent), and a spent source is skipped while the
    others still answer.
    """
    if not isinstance(sources, (list, tuple)):
        sources = [sources]
    seen: set[str] = set()
    #: A wall on one source costs that source's answer, not the axis: the next one is asked.
    #: When nothing at all came back the row was not really expanded, so the failure is raised
    #: and the caller can retry it — the first reason, from the preferred source.
    failed: Exception | None = None
    handed = False
    #: An axis that every live source failed on, whatever the other axes managed.
    lost_axes: list[Exception] = []
    #: Filling a bare row costs a request too, so a spent site is left out of the pool.
    pool = [s for s in sources if not _spent(counters, _site(s))]
    for axis in AXES:
        room = min(int(caps.get(axis, 0)), int(counters.get(axis, 0)))
        askers = [s for s in sources
                  if axis in getattr(s, "capabilities", set()) and target_for(s, item)]
        # A spent site is left out of the divisor too, or its share would go to nobody.
        live = [s for s in askers if not _spent(counters, _site(s))]
        axis_handed, axis_failed = False, False
        for index, source in enumerate(live):
            if room <= 0:
                break
            # The ask is shared out rather than first-come-first-served: one source's full
            # bibliography would otherwise spend the whole room and the rest never get asked.
            # Shares are rounded up so none goes to zero, and a short answer hands its leftover
            # to the next source instead of losing it.
            share = -(-room // (len(live) - index))
            site = _site(source)
            if site in counters:
                counters[site] = int(counters[site]) - 1
                if attempts is not None:
                    attempts[site] = int(attempts.get(site, 0)) + 1
            target = target_for(source, item)
            try:
                found = getattr(source, GRAPH_AXES[axis])(target, limit=share)
            except NotSupportedError:
                # No request was made, so the attempt charge goes back.
                if site in counters:
                    counters[site] = int(counters[site]) + 1
                    if attempts is not None:
                        attempts[site] = int(attempts.get(site, 0)) - 1
                continue
            except (LitcliError, RuntimeError, AttributeError, KeyError, ValueError,
                    TypeError) as e:
                failed = failed or e
                axis_failed = True
                continue
            # ``share`` is also the per-source ceiling, so a source that ignores ``limit``
            # cannot eat the next one's ration. Only handed rows count: a duplicate costs the
            # share nothing, or a heavily overlapping source would spend it on repeats.
            taken = 0
            for candidate in list(found):
                if room <= 0 or taken >= share:
                    break
                mine = identity(candidate)
                if mine in seen:
                    continue
                seen.add(mine)
                taken += 1
                room -= 1
                counters[axis] = int(counters.get(axis, 0)) - 1
                handed = axis_handed = True
                yield _titled(candidate, pool), axis
        if axis_failed and not axis_handed and live:
            # Whatever the other axes returned, this axis was not really read: the row must be
            # retried rather than counted as expanded and left one direction short.
            lost_axes.append(failed)  # type: ignore[arg-type]
    if failed is not None and (not handed or lost_axes):
        raise failed
