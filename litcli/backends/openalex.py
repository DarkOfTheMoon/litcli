"""OpenAlex backend: the open scholarly graph (api.openalex.org), global metadata for
papers/books/patents.

Read-only. Search (works?search=) or by DOI (works/doi:...). abstract is given as an inverted
index and must be rebuilt; best_oa_location provides **legal open-access (OA) PDF links** as a
compliant full-text source that does not go through Sci-Hub.
"""
from __future__ import annotations

import concurrent.futures
import http.client
import re
import urllib.parse
from typing import ClassVar

from ..aggregate import cited_key
from ..errors import LitcliError, NotFound
from ..models import AcademicItem
from .http import HttpBackend

_API = "https://api.openalex.org/works"

_TYPE_MAP = {
    "article": "article", "review": "article", "preprint": "article",
    "erratum": "article", "editorial": "article", "letter": "article",
    "book": "book", "book-chapter": "chapter", "monograph": "book",
    "dissertation": "thesis", "report": "report", "dataset": "dataset",
    "grant": "document", "paratext": "document", "component": "document",
    "patent": "patent", "standard": "standard",
}


def _inverted_to_text(ai: dict) -> str:
    """Rebuild OpenAlex's inverted index {word:[pos]} into readable sentences (approximate)."""
    positions = []
    for w, idxs in (ai or {}).items():
        for i in idxs:
            positions.append((i, w))
    positions.sort()
    seen = set()
    words = []
    for i, w in positions:
        if i in seen:
            continue
        seen.add(i)
        words.append(w)
    return " ".join(words)


def _creators(rec: dict) -> list[dict]:
    out = []
    for a in rec.get("authorships") or []:
        name = (a.get("author") or {}).get("display_name", "") or ""
        parts = name.split()
        if not parts:
            continue
        if len(parts) > 1:
            out.append({"family": parts[-1], "given": " ".join(parts[:-1])})
        else:
            out.append({"family": parts[0], "given": ""})
    return out


def _oa_pdf(rec: dict) -> list[str]:
    loc = rec.get("best_oa_location") or rec.get("primary_location") or {}
    pdf = loc.get("pdf_url")
    return [pdf] if pdf else []


def _oa_landing(rec: dict) -> str:
    """The work's canonical landing page (best_oa_location/primary_location), if any."""
    loc = rec.get("best_oa_location") or rec.get("primary_location") or {}
    return loc.get("landing_page_url") or ""


def _bare_doi(value: str) -> str:
    """OpenAlex's doi is a full URL (https://doi.org/10.x); strip it to a bare DOI for identity keys/export."""
    d = (value or "").strip()
    for prefix in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/"):
        if d.lower().startswith(prefix):
            return d[len(prefix):]
    return d


def _wid(value: str) -> str:
    """Extract the trailing W-id from an OpenAlex id (``https://openalex.org/W...`` or bare ``W...``)."""
    return (value or "").rstrip("/").split("/")[-1]


def _journal_name(rec: dict) -> str:
    src = (rec.get("primary_location") or {}).get("source") or {}
    return src.get("display_name", "") or ""


def _biblio(rec: dict) -> tuple[str, str, str]:
    """OpenAlex biblio object -> (volume, issue, page); page is assembled as first_page-last_page."""
    b = rec.get("biblio") or {}
    volume = b.get("volume") or ""
    issue = b.get("issue") or ""
    fp = b.get("first_page") or ""
    lp = b.get("last_page") or ""
    page = f"{fp}-{lp}" if fp and lp else fp
    return volume, issue, page


def _work_to_item(rec: dict, source: str = "openalex") -> AcademicItem:
    doi = _bare_doi(rec.get("doi") or "")
    title = rec.get("display_name", "") or ""
    volume, issue, page = _biblio(rec)
    # ``url`` is a real resolvable landing URL (consistently with the other backends), not a bare
    # DOI: prefer https://doi.org/<doi>, then the OA landing page, then the work's openalex.org page.
    url = f"https://doi.org/{doi}" if doi else (_oa_landing(rec) or rec.get("id", ""))
    return AcademicItem(
        source=source,
        key=doi,
        title=title,
        creators=_creators(rec),
        year=str(rec.get("publication_year") or ""),
        abstract=_inverted_to_text(rec.get("abstract_inverted_index")),
        doi=doi,
        url=url,
        container_title=_journal_name(rec),
        volume=volume,
        issue=issue,
        page=page,
        publisher=rec.get("publisher", "") or "",
        cited_by_count=rec.get("cited_by_count") or 0,
        item_type=_TYPE_MAP.get(rec.get("type", ""), "document"),
        attachments=_oa_pdf(rec),
        # Keep the internal OpenAlex id (W...) so ``related`` can source inline related_works.
        extra={"openalex_id": rec.get("id", "")},
    )


class OpenAlexBackend(HttpBackend):
    headers: ClassVar[dict] = {"Accept": "application/json"}
    http2 = True

    def __init__(self, name: str = "openalex", mailto: str = "",
                 base: str = _API, max_results: int = 10, rate_per_sec: float = 10.0):
        super().__init__(name=name, rate=int(rate_per_sec) or 10,
                         min_interval=1.0 / rate_per_sec if rate_per_sec > 0 else 0.0)
        self._base = base.rstrip("/")
        self._mailto = mailto
        self._max = max_results
        # Polite pool: the address rides in the UA too, not only in the mailto parameter.
        self.UA = f"litcli/0.1 (mailto: {mailto or 'unknown'})"

    @property
    def capabilities(self) -> set[str]:
        return {"read"}


    def _with_mailto(self, url: str) -> str:
        """Append the polite ``&mailto=`` param (OpenAlex asks for it) to a URL."""
        if not self._mailto:
            return url
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}mailto={urllib.parse.quote(self._mailto)}"

    def _work_url(self, key: str) -> str:
        """Build a ``/works/...`` URL from a DOI, a bare W-id, or an openalex.org URL.

        Handles the exact key forms the CLI emits/serializes: ``https://openalex.org/W...``
        (extra["openalex_id"]), a bare ``W...`` id, or a DOI (``10...`` / ``https://doi.org/...``).
        DOIs legitimately contain ``/``, so only a domain-prefixed/openalex W-id collapses to
        its final path segment.
        """
        k = (key or "").strip()
        for prefix in ("https://openalex.org/", "http://openalex.org/",
                       "https://api.openalex.org/works/"):
            if k.startswith(prefix):
                k = k[len(prefix):]
                break
        if k.lower().startswith(("https://doi.org/", "http://doi.org/")):
            k = k.split("doi.org/", 1)[1]
        if k.startswith("W") and "/" in k:
            k = k.rsplit("/", 1)[-1]
        k = k.rstrip("/")
        if k.startswith("10."):
            return f"{self._base}/doi:{urllib.parse.quote(k, safe='')}"
        return f"{self._base}/{urllib.parse.quote(k, safe='')}"

    def _work(self, key: str) -> dict:
        """Fetch one raw work dict (single GET): DOI/W-id integration + key-miss guard."""
        data = self.fetch_json(self._with_mailto(self._work_url(key)))
        if not data:
            raise KeyError(f"[{self.name}] not found: {key}")
        return data

    def search(self, query: str, limit: int = 10, filters: dict | None = None) -> list[AcademicItem]:
        filters = self._filters(filters)
        # Free query + container term go into the free-text ``search`` (OpenAlex has no
        # hard container filter; ``primary_location.source.display_name.search`` 400s).
        terms = [t for t in (query, filters.get("container")) if t]
        params: dict = {"search": " ".join(terms), "per_page": limit}
        oa_filters = []
        # OpenAlex splits ``filter`` on commas, so a value containing one (e.g. author
        # "Bonaventura, J.") must be double-quoted or the whole query 400s.
        for field, key in (("title.search", "title"), ("raw_author_name.search", "author")):
            if filters.get(key):
                oa_filters.append(f'{field}:"{filters[key]}"')
        if filters.get("year") and filters["year"].isdigit():
            oa_filters.append(f"publication_year:{filters['year']}")
        if oa_filters:
            params["filter"] = ",".join(oa_filters)
        qs = urllib.parse.urlencode(params)
        data = self.fetch_json(self._with_mailto(f"{self._base}?{qs}"))
        return [_work_to_item(w, self.name) for w in (data.get("results") or [])]

    def _fetch_works(self, ids: list[str]) -> list[AcademicItem]:
        """Parallel-fetch W-ids into AcademicItems, preserving id order, skipping per-id failures."""
        ids = [i for i in ids if i]
        if not ids:
            return []
        results: dict[str, AcademicItem] = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(ids))) as ex:
            futures = {ex.submit(self.fetch_json, self._with_mailto(self._work_url(i))): i
                       for i in ids}
            for fut in concurrent.futures.as_completed(futures):
                i = futures[fut]
                try:
                    results[i] = _work_to_item(fut.result(), self.name)
                except (LitcliError, RuntimeError, http.client.HTTPException, OSError, ValueError):
                    # Skip per-id failures (HTTP 404/5xx, non-JSON body, truncated/timeout).
                    continue
        return [results[i] for i in ids if i in results]

    def _inline_works(self, key: str, field: str, limit: int = 5,
                      sort_cited: bool = False) -> list[AcademicItem]:
        """Fetch a parent work's inline W-id list (``field``) in parallel into items.

        ``sort_cited`` ranks by citation count so the most important works surface: it
        fetches the FULL inline set, then sorts and truncates to ``limit`` (a truncated
        mid-list top-``limit`` would silently drop the highest-cited works past ``limit``).
        Without it the natural inline order is kept and only the first ``limit`` ids are
        fetched. Per-work fetch failures are skipped.
        """
        limit = max(0, limit)
        rec = self._work(key)
        ids = [_wid(r) for r in (rec.get(field) or [])]
        if sort_cited:
            items = self._fetch_works(ids)
            items.sort(key=cited_key)
            return items[:limit]
        return self._fetch_works(ids[:limit])[:limit]

    def related(self, key: str, limit: int = 5, sort_cited: bool = False) -> list[AcademicItem]:
        """Return works semantically related to ``key`` (OpenAlex ``related_works``).

        Reads the parent work's inline ``related_works`` (a list of W-ids) and fetches
        each in parallel; ``/works/{id}/related`` returns empty here, so it is unused.
        Per-work fetch failures are skipped. Order follows the inline list unless
        ``sort_cited`` ranks by citation count (which fetches the full set first).
        """
        return self._inline_works(key, "related_works", limit, sort_cited)

    def references(self, key: str, limit: int = 5, sort_cited: bool = True) -> list[AcademicItem]:
        """Return the works ``key`` cites (backward bibliography: inline ``referenced_works``).

        The foundational works a paper builds on; the FULL inline set is fetched in
        parallel and ranked by citation count so the most-cited foundations surface
        before truncating to ``limit``. ``sort_cited`` keeps the uniform edge signature
        but the by-citation ranking is unconditional (the bibliography contract).
        """
        return self._inline_works(key, "referenced_works", limit, True)

    def citing(self, key: str, limit: int = 5, sort_cited: bool = True) -> list[AcademicItem]:
        """Return works that cite ``key`` (forward), via ``/works?filter=cites:<wid>``.

        The citing works arrive as full records in one request (no per-work fetch);
        sorted server-side by citation count so the most impactful follow-ups come first.
        ``sort_cited`` is part of the uniform edge signature but the ranking is always
        honored. A bare ``W...`` / ``https://openalex.org/W...`` key skips the parent GET;
        a DOI key resolves via ``_work``.
        """
        limit = max(0, limit)
        per_page = min(max(limit, 1), 200)  # OpenAlex per_page is bounded to [1, 200].
        wid = None
        if not key.strip().startswith("10."):
            candidate = _wid(key)
            if re.fullmatch(r"W\d+", candidate):
                wid = candidate
        if wid is None:
            rec = self._work(key)
            wid = _wid(rec.get("id") or "")
        if not wid:
            raise NotFound(f"[{self.name}] unable to resolve an internal OpenAlex id for {key}")
        data = self.fetch_json(self._with_mailto(
            f"{self._base}?filter=cites:{wid}&per_page={per_page}&sort=cited_by_count:desc"))
        return [_work_to_item(w, self.name) for w in (data.get("results") or [])][:limit]

    def get(self, key: str) -> AcademicItem:
        return _work_to_item(self._work(key), self.name)

    def get_attachment(self, key: str) -> bytes:
        """Download a legal OA PDF link (best_oa_location's pdf_url)."""
        item = self.get(key)
        if not item.attachments:
            raise NotFound(f"[{self.name}] no OA PDF link for {key}")
        from .. import pdf as pdfutil
        return pdfutil.download(item.attachments[0])
