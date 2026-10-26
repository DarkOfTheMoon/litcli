"""Cross-backend aggregation domain layer: dedupe/merge/sort, backend-agnostic.

Different backends (e.g. Zotero vs arXiv) may return different metadata snapshots
of the same work. This module provides pure functions: normalization, identity key,
information-completeness scoring, and merging, shared by the ``--all`` aggregate
search. It touches no backend and depends on no network.
"""
from __future__ import annotations

import copy
import re

from .models import AcademicItem

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")


def norm_title(title: str) -> str:
    """Normalize a title: lowercase, strip punctuation, collapse whitespace."""
    t = _PUNCT_RE.sub(" ", (title or "").lower())
    return _WS_RE.sub(" ", t).strip()


def norm_doi(doi: str) -> str:
    """Normalize a DOI: strip, lowercase, drop trailing periods, and strip the https://doi.org/ prefix.

    Different backends return DOIs in different shapes: Crossref gives a bare DOI
    (10.1103/x), OpenAlex a full URL (https://doi.org/10.1103/x). Keeping the prefix
    makes identity keys differ, so the ``--all`` aggregate cannot merge crossref/openalex
    records of the same work (see test_norm_doi_url_prefix).
    """
    d = (doi or "").strip().lower().rstrip(".")
    for prefix in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/",
                   "http://dx.doi.org/"):
        if d.startswith(prefix):
            d = d[len(prefix):]
            break
    return d.rstrip(".")


def norm_isbn(isbn: str) -> str:
    """Normalize ISBN: strip whitespace/hyphens, lowercase, drop trailing periods.

    ISBNs arrive with varying separators (``978-7-118-04834-8`` vs `9787118048348`);
    keeping them distinct splits the same book across backends. Stripping the separators
    makes the identity key stable (mirror of :func:`norm_doi`).
    """
    return re.sub(r"[^0-9xX]", "", (isbn or "")).lower()


def identity(item: AcademicItem) -> str:
    """Identity key: prefer an effective DOI, then ISBN (books), else the normalized title.

    Used to judge sameness across libraries. Journal articles / papers carry a DOI; books
    typically carry an ISBN and no DOI, so ISBN is the book counterpart of DOI.
    """
    d = norm_doi(item.doi)
    if d:
        return "doi:" + d
    b = norm_isbn(item.isbn)
    if b:
        return "isbn:" + b
    return "title:" + norm_title(item.title)


def _info_score(item: AcademicItem) -> tuple:
    """Information-completeness score (coarse): add per present abstract/doi/isbn/url, with title length as the tie-breaker."""
    return (
        1 if item.abstract else 0,
        1 if item.doi else 0,
        1 if item.isbn else 0,
        1 if item.url else 0,
        len(item.title or ""),
    )


def _merge_two(primary: AcademicItem, other: AcademicItem) -> None:
    """Fill primary's missing fields from other in place (primary is already a copy, so it is safe to modify)."""
    if not primary.creators and other.creators:
        primary.creators = list(other.creators)
    if not primary.year and other.year:
        primary.year = other.year
    if not primary.abstract and other.abstract:
        primary.abstract = other.abstract
    if not primary.url and other.url:
        primary.url = other.url
    if not primary.container_title and other.container_title:
        primary.container_title = other.container_title
    if not primary.volume and other.volume:
        primary.volume = other.volume
    if not primary.issue and other.issue:
        primary.issue = other.issue
    if not primary.page and other.page:
        primary.page = other.page
    if not primary.publisher and other.publisher:
        primary.publisher = other.publisher
    if not primary.isbn and other.isbn:
        primary.isbn = other.isbn
    if not primary.edition and other.edition:
        primary.edition = other.edition
    if other.cited_by_count > primary.cited_by_count:
        primary.cited_by_count = other.cited_by_count
    oaid = (other.extra or {}).get("openalex_id")
    if oaid and not (primary.extra or {}).get("openalex_id"):
        primary.extra["openalex_id"] = oaid
    for a in other.attachments:
        if a not in primary.attachments:
            primary.attachments.append(a)


def _year_key(year: str) -> int:
    """Descending year sort key (non-numeric treated as 0)."""
    return -int(year) if year.isdigit() else 0


def cited_key(item: AcademicItem) -> int:
    """Citation-count descending sort key (treats no-citation records as 0). Public so backends
    and the CLI can rank by citation without importing a private symbol."""
    return -item.cited_by_count


def merge_items(items: list[AcademicItem]) -> list[AcademicItem]:
    """Dedupe + merge + sort.

    - Group by :func:`identity`, preserving first-occurrence order;
    - within each group, take the most complete record as the primary and fill the rest's
      missing fields from it (copy operations, do not mutate the inputs);
    - ``extra["sources"]`` records all sources, while ``source`` keeps the primary's source;
    - finally sort by year descending, then by title ascending (stable).
    """
    groups: dict[str, list[AcademicItem]] = {}
    order: list[str] = []
    for it in items:
        k = identity(it)
        if k not in groups:
            groups[k] = []
            order.append(k)
        groups[k].append(it)

    merged: list[AcademicItem] = []
    for k in order:
        group = groups[k]
        primary = copy.deepcopy(max(group, key=_info_score))
        for other in group:
            if other is primary:
                continue
            _merge_two(primary, other)
        srcs = sorted({it.source for it in group if it.source})
        if srcs:
            primary.extra["sources"] = srcs
        merged.append(primary)

    merged.sort(key=lambda it: (_year_key(it.year), norm_title(it.title)))
    return merged
