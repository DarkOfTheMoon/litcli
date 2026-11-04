"""Crossref backend: the authoritative open metadata API (api.crossref.org).

Covers DOI-registered metadata for papers/books/proceedings/patents and more; read-only. No key
needed (a mailto obtains a better quota). Search by title, or fetch a single record by DOI.
Normalized to AcademicItem.
"""
from __future__ import annotations

import re
import urllib.parse
from typing import ClassVar

from ..models import AcademicItem
from .http import HttpBackend

_API = "https://api.crossref.org/works"

# Crossref type -> AcademicItem.item_type (CSL type)
_TYPE_MAP = {
    "journal-article": "article",
    "proceedings-article": "paper-conference",
    "book": "book",
    "book-chapter": "chapter",
    "book-section": "chapter",
    "monograph": "book",
    "dissertation": "thesis",
    "report": "report",
    "posted-content": "article",        # preprint
    "patent": "patent",
    "reference-entry": "document",
    "standard": "standard",
}

_XML_RE = re.compile(r"<[^>]+>")

def _strip_xml(s: str) -> str:
    """Strip JATS/HTML tags (often present in Crossref abstracts)."""
    return _XML_RE.sub(" ", s or "").strip()


def _authors(data: dict) -> list[dict]:
    out = []
    for a in data.get("author") or []:
        fam = a.get("family", "")
        giv = a.get("given", "")
        if fam or giv:
            out.append({"family": fam, "given": giv})
    return out


def _pdf_links(item: dict) -> list[str]:
    return [ln["URL"] for ln in (item.get("link") or [])
            if ln.get("content-type") in ("application/pdf", "application/x-pdf") and ln.get("URL")]


def _item_to_academic(rec: dict, source: str = "crossref") -> AcademicItem:
    title = (rec.get("title") or [""])[0]
    year = ""
    for k in ("published", "issued", "created", "updated"):
        dp = (rec.get(k) or {}).get("date-parts") or []
        if dp and dp[0] and dp[0][0]:
            year = str(dp[0][0])
            break
    container = (rec.get("container-title") or [""])[0]
    return AcademicItem(
        source=source,
        key=rec.get("DOI", ""),
        title=title,
        creators=_authors(rec),
        year=year,
        abstract=_strip_xml(rec.get("abstract", "")),
        doi=rec.get("DOI", ""),
        url=rec.get("URL", ""),
        container_title=container,
        volume=rec.get("volume", "") or "",
        issue=rec.get("issue", "") or "",
        page=rec.get("page", "") or "",
        publisher=rec.get("publisher", "") or "",
        cited_by_count=rec.get("is-referenced-by-count") or 0,
        item_type=_TYPE_MAP.get(rec.get("type", ""), "document"),
        attachments=_pdf_links(rec),
    )


class CrossrefBackend(HttpBackend):
    headers: ClassVar[dict] = {"Accept": "application/json"}
    http2 = True

    def __init__(self, name: str = "crossref", mailto: str = "",
                 base: str = _API, max_results: int = 10):
        super().__init__(name=name, rate=10, min_interval=0.1)
        self._base = base.rstrip("/")
        self._mailto = mailto
        self._max = max_results
        self.UA = f"litcli/0.1 (contact: {mailto or 'none'})"

    @property
    def capabilities(self) -> set[str]:
        return {"read"}


    def search(self, query: str, limit: int = 10, filters: dict | None = None) -> list[AcademicItem]:
        filters = self._filters(filters)
        params: dict = {"query.bibliographic": query, "rows": limit}
        for key, qparam in (("title", "query.title"),
                            ("author", "query.author"),
                            ("container", "query.container-title")):
            val = filters.get(key)
            if val:
                params[qparam] = val
        year = filters.get("year")
        if year and year.isdigit():
            params["filter"] = f"from-pub-date:{year}-01-01,until-pub-date:{year}-12-31"
        data = self.fetch_json(f"{self._base}?{urllib.parse.urlencode(params)}")
        items = data.get("message", {}).get("items") or []
        return [_item_to_academic(i, self.name) for i in items]

    def get(self, key: str) -> AcademicItem:
        doi = urllib.parse.quote(key, safe="")
        data = self.fetch_json(f"{self._base}/{doi}")
        msg = data.get("message")
        if not msg:
            raise KeyError(f"[{self.name}] not found: {key}")
        return _item_to_academic(msg, self.name)
