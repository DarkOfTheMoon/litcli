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


def _first_author(item: AcademicItem) -> str:
    for c in item.creators:
        name = _WS_RE.sub(" ", (c.get("family") or "").lower()).strip()
        if name:
            return name
    return ""


def weak_identity(item: AcademicItem) -> str:
    """Title + first author, for records with no DOI/ISBN.

    A title alone is too loose (``Editorial``, ``Introduction`` recur) and a title that is too
    tight fails to merge the same preprint coming from two repositories, so the author surname
    breaks the tie without needing a network lookup.
    """
    return f"{norm_title(item.title)}|{_first_author(item)}"


def _year_ok(one: str, other: str) -> bool:
    """Same year, or one of them unknown: a preprint and its published version are one work."""
    a, b = (one or "")[:4], (other or "")[:4]
    if not (a.isdigit() and b.isdigit()):
        return True
    return abs(int(a) - int(b)) <= 1


def _joinable(item: AcademicItem, groups: dict, weak_index: dict) -> str | None:
    """The single group a DOI-less record belongs to, if exactly one claims its title+author."""
    weak = weak_identity(item)
    hits = {gid for gid, keys in weak_index.items()
            if weak in keys and any(_year_ok(item.year, m.year) for m in groups[gid])}
    return hits.pop() if len(hits) == 1 else None


def identity(item: AcademicItem) -> str:
    """Identity key: prefer an effective DOI, then ISBN (books), else the weak title+author key.

    Used to judge sameness across libraries. Journal articles / papers carry a DOI; books
    typically carry an ISBN and no DOI, so ISBN is the book counterpart of DOI.
    """
    d = norm_doi(item.doi)
    if d:
        return "doi:" + d
    b = norm_isbn(item.isbn)
    if b:
        return "isbn:" + b
    return "title:" + weak_identity(item)


def _info_score(item: AcademicItem) -> tuple:
    """Information-completeness score (coarse): identifiers first, then abstract/url, with title
    length as the tie-breaker.

    An identifier outranks an abstract on purpose: the record carrying the DOI is the one whose
    key must survive the merge, or the result is an arXiv id that ``export``/``fetch`` cannot use
    even though the DOI is known.
    """
    return (
        1 if item.doi else 0,
        1 if item.isbn else 0,
        1 if item.abstract else 0,
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
    if not primary.doi and other.doi:
        primary.doi = other.doi
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
    - a DOI-less record joins the single strong group with the same title and author (and a
      compatible year) rather than forming a second copy of the same work;
    - within each group, take the most complete record as the primary and fill the rest's
      missing fields from it (copy operations, do not mutate the inputs);
    - citation counts disagree per source: the largest is kept for ranking and every claim is
      recorded in ``extra["cited_by_source"]``;
    - ``extra["sources"]`` records all sources, while ``source`` keeps the primary's source;
    - finally sort by year descending, then by title ascending (stable).
    """
    # Strong identifiers first: a DOI/ISBN group must exist before a DOI-less twin can find it.
    strong = [it for it in items if not identity(it).startswith("title:")]
    weak_only = [it for it in items if identity(it).startswith("title:")]

    groups: dict[str, list[AcademicItem]] = {}
    order: list[str] = []
    weak_index: dict[str, list[str]] = {}

    def add(k: str, it: AcademicItem) -> None:
        if k not in groups:
            groups[k] = []
            order.append(k)
        groups[k].append(it)
        weak_index.setdefault(k, []).append(weak_identity(it))

    for it in strong:
        add(identity(it), it)
    for it in weak_only:
        k = _joinable(it, groups, weak_index)
        if k is None:
            k = identity(it)
            if k in groups:  # same title and author, but not the same work (years far apart)
                k = f"{k}#{len(order)}"
        add(k, it)

    merged: list[AcademicItem] = []
    for k in order:
        group = groups[k]
        primary = max(group, key=_info_score)
        others = [it for it in group if it is not primary]
        claims: dict[str, int] = {}
        for member in group:
            if member.cited_by_count and member.source:
                claims.setdefault(member.source, member.cited_by_count)
        primary = copy.deepcopy(primary)
        if claims:
            # Counts disagree per source; keep the largest for ranking, remember every claim.
            primary.extra["cited_by_source"] = claims
        for other in others:
            _merge_two(primary, other)
        srcs = sorted({it.source for it in group if it.source})
        if srcs:
            primary.extra["sources"] = srcs
        merged.append(primary)

    merged.sort(key=lambda it: (_year_key(it.year), norm_title(it.title)))
    return merged
