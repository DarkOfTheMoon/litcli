"""Decide whether a candidate may enter the collection — and say why, either way.

The order is cheapest and most decisive first: identity, then "already judged", then the hard
filters the user set (year window, container), and only then the score. The score is not
computed here — it is the caller's job, because it needs the collection and the trace;
``decide`` reads it from ``item.extra["score"]`` so the reason can quote the real number
instead of a restatement.

Every verdict carries the identity it judged and a reason. Seeds are the one unconditional
case: they were chosen by the user (or by the opening search), so they skip the filters and the
threshold and are recorded as ``seed``.

How many a hop may keep is a second question, answered by :func:`rank` once the whole hop is
judged: the threshold is a floor against junk, the envelope's ``admit`` is the volume, and
"which of these" is settled by the score rather than by which source answered first.
"""
from __future__ import annotations

from .aggregate import identity
from .errors import BadInput
from .models import AcademicItem
from .project import DEFAULT_THRESHOLD, Project


def _year(text: str) -> int | None:
    try:
        return int(str(text).strip()[:4])
    except (TypeError, ValueError):
        return None


def _in_window(item: AcademicItem, project: Project) -> bool:
    """False only when a bound exists and the item is outside it; an unknown year passes."""
    year = _year(item.year)
    low, high = _year(project.year_from), _year(project.year_to)
    if year is None:
        return True
    if low is not None and year < low:
        return False
    return not (high is not None and year > high)


def _verdict(admit: bool, reason: str, key: str) -> dict:
    return {"admit": admit, "reason": reason, "identity": key}


def decide(item: AcademicItem, project: Project, seen: set[str]) -> dict:
    """``{"admit", "reason", "identity"}`` for one candidate."""
    key = identity(item)
    if key in seen:
        return _verdict(False, "seen", key)
    if item.extra.get("seed"):
        return _verdict(True, "seed", key)
    if not _in_window(item, project):
        return _verdict(False, f"year {item.year} outside "
                              f"{project.year_from or '…'}-{project.year_to or '…'}", key)
    wanted = (project.container or "").strip()
    if wanted and wanted.lower() not in (item.container_title or "").lower():
        return _verdict(False, f"container {item.container_title!r} lacks {wanted!r}", key)
    score = item.extra.get("score")
    if score is None:
        raise BadInput(f"candidate {key} reached admission without a score")
    threshold = float(project.threshold or DEFAULT_THRESHOLD)
    if score < threshold:
        return _verdict(False, f"score {score:.2f} < {threshold:.2f}", key)
    return _verdict(True, f"score {score:.2f} >= {threshold:.2f}", key)


def rank(rows: list[dict], cap: int) -> None:
    """Keep at most ``cap`` of a hop's admitted rows, best score first, and say which lost.

    ``rows`` are the hop's verdict records in arrival order, each carrying its ``score``; the
    sort is stable, so a tie keeps that order. Rows already refused keep their own reason — a
    demotion only ever turns an ``admit`` off, never on.
    """
    admitted = [row for row in rows if row["admit"]]
    for place, row in enumerate(sorted(admitted, key=lambda r: -float(r.get("score") or 0.0))):
        if place < cap:
            continue
        row["admit"] = False
        row["reason"] = f"rank {place + 1} of {len(admitted)} for cap {cap}"
