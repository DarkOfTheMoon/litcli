"""Rank a candidate by what it shares with the question and the cluster, then by time, then by
its own standing.

Five axes, each normalized to ``0..1`` *before* its weight is applied, so a weight only ever
moves its own axis and a recorded score can be re-derived by hand:

* ``c`` — the cluster skeleton: what share of the candidate's own bibliography it shares with
  what is already collected (co-citation). Divided by its own reference count, because ten shared
  references are a tenth of a 400-reference review but half of a focused 20-reference paper —
  unnormalized, any long review saturates the axis on any topic. This is the best evidence of
  the topic once the collection is on it, so it gets the largest weight.
* ``q`` — the question: how much of it the candidate itself carries, as the *square* of the
  share of its content words (stopwords excluded). Squared, because carrying half of what was
  asked is not half an answer — one word of a seven-word question says almost nothing, and a
  generic review would otherwise ride its citation count. ``c`` only ever says "like what we
  collected", so a collection that starts drifting pulls its own neighbours in and the drift
  compounds; ``q`` is the anchor that does not move with it, and the only axis that works on the
  first hop or for a record carrying nothing but a title.
* ``b`` — recency: a linear position inside the project's year window, halved so a candidate
  that cites one of the seeds can pull above the window's own top.
* ``a`` — classicness, the fallback: citations per year of age. It only speaks loudly while
  the cluster is sparse (few new candidates lately), which is exactly when the skeleton has
  nothing to say.

The last axis, ``d``, is the declared article type: a project weighs the types it wants
(``--type review`` writes 2.0) and everything unlisted falls back to a floor.

The total is a *ranking*; admission reads :func:`evidence` instead (see below). ``q``, ``b``
and ``d`` are properties of the item and the project alone; nothing here does I/O.
"""
from __future__ import annotations

from .commands.search import coverage, overlap
from .models import AcademicItem
from .project import Project

#: Weights of the shared axes. The question sits above recency and below the cluster: it is
#: the second-best evidence there is, and ``a``'s weight is the sparse one.
W_CLUSTER = 3.0
W_QUESTION = 2.0
W_RECENCY = 1.0
W_CLASSIC = 0.5
W_CLASSIC_SPARSE = 1.5

#: Research-grade types: an undeclared candidate of one of these keeps a full type weight.
DEFAULT_TYPES = frozenset({"article", "review", "preprint", "book", "book-chapter", "chapter",
                           "monograph"})
#: An undeclared type outside that set still counts, but well below it.
UNLISTED_TYPE_WEIGHT = 0.2

#: References shared with the collection that saturate ``c``; also the floor of its denominator,
#: so a short bibliography is not punished for being short.
CLUSTER_CAP = 10
#: Works inside the collection that have to cite the candidate to saturate that signal.
CITERS_CAP = 3
#: Citations per year of age that saturate ``a``.
PER_YEAR_CAP = 20
#: A candidate citing a seed gets this much on top of its window position, capped at 1.
SEED_CITER_BONUS = 0.5
#: No window: a neutral recency, so the seed-citer bonus still has headroom.
NEUTRAL_RECENCY = 0.5
#: Hops that must each add fewer than ``SPARSE_NEW`` candidates were called sparse.
SPARSE_HOPS = 3
SPARSE_NEW = 2
#: The current year, fixed so a score is reproducible across a new year's eve.
CURRENT_YEAR = 2026


def _year(text: str) -> int | None:
    try:
        return int(str(text).strip()[:4])
    except (TypeError, ValueError):
        return None


def _window(project: Project) -> tuple[int | None, int | None]:
    return _year(project.year_from), _year(project.year_to)


def _recency(item: AcademicItem, project: Project, seeds: set[str]) -> float:
    """Linear position of the item's year in the window, plus the seed-citer bonus."""
    low, high = _window(project)
    year = _year(item.year)
    if low is None and high is None:
        position = NEUTRAL_RECENCY
    elif year is None:
        position = 0.0
    elif low is None:
        position = 1.0 if high is None or year <= high else 0.0
    elif high is None:
        position = 1.0 if year >= low else 0.0
    elif not low <= year <= high:
        position = 0.0
    elif high == low:
        position = 1.0
    else:
        position = (year - low) / (high - low)
    if _cites_seed(item, seeds):
        position = min(1.0, position + SEED_CITER_BONUS)
    return position


def bare(work_id: str) -> str:
    """The bare work id of a reference, whatever its spelling.

    ``W22``, ``openalex:W22`` and ``https://openalex.org/W22`` all come back as ``W22``: the
    project stores seeds as ids while the graph answers with full URLs, so the two sides of the
    comparison have to be spelled alike.
    """
    tail = str(work_id).strip().rstrip("/").rsplit("/", 1)[-1]
    return tail.partition(":")[2] or tail


def _cites_seed(item: AcademicItem, seeds: set[str]) -> bool:
    # The seed-citer bonus is a bonus above the window, so it has to work for a real seed.
    if not seeds:
        return False
    wanted = {bare(seed) for seed in seeds}
    cited = {bare(ref) for ref in (item.extra.get("referenced_works") or [])}
    return bool(wanted & cited)


def _classicness(item: AcademicItem) -> float:
    """Citations per year of age, saturated at ``PER_YEAR_CAP``."""
    year = _year(item.year)
    age = max(1, CURRENT_YEAR - year) if year else 1
    return min(1.0, (item.cited_by_count or 0) / age / PER_YEAR_CAP)


def _type_weight(item: AcademicItem, project: Project) -> float:
    """How the project rates this item's article type; the floor is :data:`UNLISTED_TYPE_WEIGHT`."""
    kind = str(item.extra.get("openalex_type") or "")
    if kind and kind in project.type_weight:
        return float(project.type_weight[kind])
    return 1.0 if kind in DEFAULT_TYPES else UNLISTED_TYPE_WEIGHT


def sparse(project: Project, trace: list[dict]) -> bool:
    """True when the last :data:`SPARSE_HOPS` hops each added fewer than :data:`SPARSE_NEW`.

    The trace carries every event, and only a hop's end record says how much it added, so the
    other events are skipped: three candidates of one productive hop are not three dry hops.
    """
    hops = [entry for entry in trace if "new" in entry]
    if len(hops) < SPARSE_HOPS:
        return False
    recent = hops[-SPARSE_HOPS:]
    return all(int(entry.get("new") or 0) < SPARSE_NEW for entry in recent)


def _refs(item: AcademicItem) -> set[str]:
    return {str(r) for r in (item.extra.get("referenced_works") or [])}


def _shared(item: AcademicItem, collected_refs: set[str]) -> int:
    return len(_refs(item) & collected_refs)


def coupling(item: AcademicItem, collected_refs: set[str]) -> float:
    """Shared references over the candidate's own bibliography size, in ``0..1``.

    The doc-frequency lesson from BM25's length normalization: a long bibliography shares a
    little with everything, so raw counts say "big review" rather than "same topic". Divided by
    the candidate's own count (never below ``CLUSTER_CAP``), 10 shared references of 400 read as
    the tenth they are.
    """
    return _shared(item, collected_refs) / max(CLUSTER_CAP, len(_refs(item)))


def _citers(item: AcademicItem, cited: dict[str, int]) -> float:
    return min(1.0, cited.get(bare(item.key), 0) / CITERS_CAP)


def evidence(item: AcademicItem, project: Project, collected_refs: set[str],
             cited: dict[str, int] | None = None) -> float:
    """What the item itself carries, in ``0..1``: the question, the cluster, or its citers.

    Recency, classicness and type are about *when* and *how well known*, never about *what*:
    a fresh, heavily cited paper on some other subject scores on all three, and all three
    together reach exactly the old default threshold (``1.0 + 0.5 + 1.0``), so a total-based
    gate let any recent well-cited work through. This one cannot: an item with no shared
    reference and no question word is worth 0 however famous it is.

    Three signals, in the order the citation-graph literature ranks them: being cited by
    something already in the collection (direct, the strongest), sharing its references
    (coupling), and carrying the question. A direct citation counts ``1 / CITERS_CAP``; coupling
    is :func:`coupling`, already normalized by the candidate's own bibliography; coverage is
    :func:`overlap`, which requires the question's own parts, not just its words.

    The first hop has no cluster and no citers: it stands on ``q`` alone.
    """
    carried = overlap(item, project.question) + coupling(item, collected_refs)
    return min(1.0, carried + _citers(item, cited or {}))


def score(item: AcademicItem, project: Project, collected_refs: set[str],
          trace: list[dict]) -> tuple[float, dict[str, float]]:
    """``(total, parts)`` — the parts are the weighted contributions and are what gets recorded.

    They sum to the total, so a ``seen.jsonl`` row explains its own verdict. Only the parts
    carry the weights; :func:`evidence` is unweighted and is what admission compares.
    """
    classic_weight = W_CLASSIC_SPARSE if sparse(project, trace) else W_CLASSIC
    parts = {
        "c": W_CLUSTER * coupling(item, collected_refs),
        "q": W_QUESTION * coverage(item, project.question),
        "b": W_RECENCY * _recency(item, project, {str(s) for s in project.seeds}),
        "a": classic_weight * _classicness(item),
        "d": _type_weight(item, project),
    }
    return round(sum(parts.values()), 6), parts
