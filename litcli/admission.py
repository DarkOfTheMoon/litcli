"""Decide whether a candidate may enter the collection — and say why, either way.

The order is cheapest and most decisive first: identity, then "already judged", then the hard
filters the user set (year window, container), and only then the evidence. Evidence is not
computed here — it is the caller's job, because it needs the collection; ``decide`` reads it
from ``item.extra["evidence"]`` so the reason can quote the real number instead of a
restatement.

Admission reads *evidence*, not the score total. Recency, classicness and type are
always present and together reach 2.5 on their own, so a gate on the total admits any recent
well-cited paper whatever it is about — and once the cluster is sparse the same three axes
reach 3.5, more than the gate. Evidence — a citation from inside the collection, a shared
reference, a part of the question — is the only thing that says the work is about what was asked.

Every verdict carries the identity it judged and a reason. Seeds are the one unconditional
case: they were chosen by the user (or by the opening search), so they skip the filters and the
floor and are recorded as ``seed``.

How many a hop may keep is a second question, answered by :func:`rank` once the whole hop is
judged: the floor keeps junk out, the envelope's ``admit`` is the volume, and
"which of these" is settled by the evidence first and the total only between equals — never by
which source answered first.
"""
from __future__ import annotations

from .aggregate import identity
from .errors import BadInput
from .models import AcademicItem
from .project import DEFAULT_FLOOR, Project


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
    evidence = item.extra.get("evidence")
    if evidence is None:
        raise BadInput(f"candidate {key} reached admission without evidence")
    # 0 is a valid floor (admit everything), so this cannot be an `or`.
    floor = float(project.floor if project.floor is not None else DEFAULT_FLOOR)
    if evidence < floor:
        return _verdict(False, f"evidence {evidence:.2f} < {floor:.2f}", key)
    return _verdict(True, f"evidence {evidence:.2f} >= {floor:.2f}", key)


def rank(rows: list[dict], cap: int) -> None:
    """Keep at most ``cap`` of a hop's admitted rows, best evidence first, and say which lost.

    ``rows`` are the hop's verdict records in arrival order, each carrying its ``evidence`` and
    ``score``. Evidence leads: a famous recent work about something else can outscore an
    on-topic one on the total, and the slot belongs to the one that answers the question. The
    sort is stable, so equal evidence and score keep the arrival order. Rows already refused
    keep their own reason — a demotion only ever turns an ``admit`` off, never on.
    """
    admitted = [row for row in rows if row["admit"]]
    best = sorted(admitted, key=lambda r: (-float(r.get("evidence") or 0.0),
                                           -float(r.get("score") or 0.0)))
    for place, row in enumerate(best):
        if place < cap:
            continue
        row["admit"] = False
        row["reason"] = f"rank {place + 1} of {len(admitted)} for cap {cap}"
