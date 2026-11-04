"""DOAJ backend: the Directory of Open Access Journals article index (doaj.org/api).

Everything in DOAJ is open access by definition, so this is the OA-only article search:
useful when a plain query on the general sources returns mostly paywalled records. Read-only,
no key. Records carry a DOI, so the normal OA fulltext chain can take over from the hit.
"""
from __future__ import annotations

import re
import urllib.parse

from ..models import AcademicItem, extract_year, normalize_author_name
from .http import HttpBackend

_API = "https://doaj.org/api/search/articles/"

# bibjson.author[].name is "Given Family"; CSL wants the split done the shared way.
# Lucene reserves these inside a term; an unescaped "?" / "&" in a fulltext URL makes DOAJ
# answer HTTP 400 "Query contains disallowed Lucene features".
_LUCENE_RESERVED_RE = re.compile(r'([?&|!{}\[\]^~*"\\])')


def _escape_term(text: str) -> str:
    """Backslash-escape Lucene specials so a URL can be used as an exact term."""
    return _LUCENE_RESERVED_RE.sub(r"\\\1", text)


def _quote(term: str) -> str:
    """Wrap a free-text term in quotes unless the caller already grouped it with parentheses."""
    t = (term or "").strip()
    if not t:
        return ""
    if t.startswith("(") or t.startswith('"'):
        return t
    return f'"{t}"'


def _creators(record: dict) -> list[dict]:
    out = []
    for a in record.get("author") or []:
        name = (a.get("name") or "").strip()
        if name:
            out.append(normalize_author_name(name))
    return out


def _doi(record: dict) -> str:
    for ident in record.get("identifier") or []:
        if ident.get("type") == "doi" and ident.get("id"):
            # Some records store the DOI as a resolver URL; keep the bare DOI.
            return re.sub(r"^https?://(?:dx\.)?doi\.org/", "", ident["id"].strip())
    return ""


def _fulltext_url(record: dict) -> str:
    for link in record.get("link") or []:
        if link.get("type") == "fulltext" and link.get("url"):
            return link["url"]
    return ""


def record_to_item(result: dict, source: str = "doaj") -> AcademicItem:
    """Normalize one DOAJ search result (pure: no I/O, offline-testable).

    The API wraps each article as ``{"bibjson": {...}}``; a bare ``bibjson`` is accepted too.
    """
    rec = result.get("bibjson") or result
    doi = _doi(rec)
    journal = rec.get("journal") or {}
    first = (rec.get("start_page") or "").strip()
    last = (rec.get("end_page") or "").strip()
    page = f"{first}-{last}" if first and last else first
    return AcademicItem(
        source=source,
        key=doi or _fulltext_url(rec),
        title=(rec.get("title") or "").strip(),
        creators=_creators(rec),
        year=extract_year(str(rec.get("year") or "")),
        abstract=(rec.get("abstract") or "").strip(),
        doi=doi,
        url=_fulltext_url(rec) or (f"https://doi.org/{doi}" if doi else ""),
        item_type="article",
        container_title=(journal.get("title") or "").strip(),
        volume=str(journal.get("volume") or ""),
        issue=str(journal.get("number") or ""),
        page=page,
        publisher=(journal.get("publisher") or "").strip(),
        extra={"issns": ", ".join(journal.get("issns") or []),
               "subjects": ", ".join(s.get("term", "") for s in rec.get("subject") or [])},
    )


class DoajBackend(HttpBackend):
    """Read-only search across open-access journal articles."""

    headers = {"Accept": "application/json"}

    def __init__(self, name: str = "doaj", base: str = _API, timeout: int = 25, delay: float = 1.0):
        super().__init__(name=name, timeout=timeout, min_interval=delay)
        self._base = base.rstrip("/") + "/"

    @property
    def capabilities(self) -> set[str]:
        return {"read"}

    def _query(self, query: str, page_size: int = 10, page: int = 1) -> list[dict]:
        """GET one result page (paced by the shared transport); returns the raw records."""
        term = urllib.parse.quote(query, safe="")
        data = self.fetch_json(f"{self._base}{term}?pageSize={page_size}&page={max(1, page)}")
        return (data or {}).get("results") or []

    def _query_pages(self, query: str, limit: int) -> list[dict]:
        """Collect up to ``limit`` records across pages (the API caps pageSize at 100)."""
        size = min(max(1, limit), 100)
        out: list[dict] = []
        for page in range(1, (limit + size - 1) // size + 1):
            batch = self._query(query, page_size=size, page=page)
            out.extend(batch)
            if len(batch) < size or len(out) >= limit:
                break
        return out

    def search(self, query: str, limit: int = 10, filters: dict | None = None) -> list[AcademicItem]:
        filters = self._filters(filters)
        parts = [_quote(query)]
        for field, key in (("bibjson.title", "title"), ("bibjson.author.name", "author"),
                           ("bibjson.journal.title", "container")):
            if filters.get(key):
                parts.append(f'{field}:{_quote(filters[key])}')
        if filters.get("year"):
            parts.append(f"bibjson.year:{filters['year']}")
        # "*" is the accepted match-all; "*:*" is rejected as a disallowed Lucene feature.
        q = " AND ".join(p for p in parts if p) or "*"
        limit = max(1, limit)
        records = self._query_pages(q, limit)
        return [record_to_item(r, self.name) for r in records][:limit]

    def get(self, key: str) -> AcademicItem:
        k = (key or "").strip()
        if k.startswith("doaj/"):
            k = k[len("doaj/"):]
        if k.startswith("https://doi.org/"):  # only the prefix, not embedded occurrences
            k = k[len("https://doi.org/"):]
        if not k:
            raise KeyError(f"[{self.name}] empty key")
        if k.startswith(("http://", "https://")):
            # Records without a DOI are keyed by their fulltext URL (see ``record_to_item``).
            query = f'bibjson.link.url:"{_escape_term(k)}"'
        else:
            query = f'doi:"{_escape_term(k)}"'
        records = self._query(query, page_size=1)
        if not records:
            raise KeyError(f"[{self.name}] no article for key {k}")
        return record_to_item(records[0], self.name)
