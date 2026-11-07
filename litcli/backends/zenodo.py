"""Zenodo backend: CERN's open-access record repository (zenodo.org/api).

Zenodo is where a lot of open datasets, software releases, reports and preprints register a
DOI, so it covers material the article indexes do not. Read-only, no key, everything is
openly licensed. Files are served by the API itself, so an OA PDF can be resolved directly.
"""
from __future__ import annotations

import html
import re
import urllib.parse
from typing import ClassVar

from ..models import AcademicItem, extract_year, normalize_author_name
from .http import HttpBackend

_API = "https://zenodo.org/api/records"

_TAG_RE = re.compile(r"<[^>]+>")
_DOI_ID_RE = re.compile(r"zenodo\.(\d+)$", re.I)
# The web/API URL forms this backend itself prints as ``url`` (copy-paste round-trip).
_URL_ID_RE = re.compile(r"zenodo\.org/(?:api/)?records/(\d+)")

_TYPE_MAP = {
    "dataset": "dataset",
    "software": "software",
    "publication": "article",
    "preprint": "article",
    "thesis": "thesis",
    "report": "report",
    "presentation": "speech",
    "image": "graphic",
    "video": "motion_picture",
    "audio": "song",
    "lesson": "article",
    "other": "document",
}


def _strip_html(text: str) -> str:
    return " ".join(html.unescape(_TAG_RE.sub(" ", text or "")).split())


def _pdf_file(record: dict) -> str:
    """URL of the first PDF file on the record, else "" (the content URL ends in ``/content``)."""
    for f in record.get("files") or []:
        link = (f.get("links") or {}).get("self") or ""
        name = (f.get("key") or "").lower()
        if link and (f.get("mimetype") == "application/pdf" or name.endswith(".pdf")):
            return link
    return ""


def record_to_item(record: dict, source: str = "zenodo") -> AcademicItem:
    """Normalize one Zenodo record (pure: no I/O, offline-testable)."""
    meta = record.get("metadata") or {}
    doi = meta.get("doi") or record.get("doi") or ""
    kind = (meta.get("resource_type") or {}).get("type") or "other"
    creators = []
    for person in meta.get("creators") or []:
        name = person.get("name") or ""
        if name:
            creators.append(normalize_author_name(name))
    links = record.get("links") or {}
    pdf = _pdf_file(record)
    return AcademicItem(
        source=source,
        key=str(record.get("id") or doi),
        title=html.unescape(meta.get("title") or ""),
        creators=creators,
        year=extract_year(meta.get("publication_date") or ""),
        abstract=_strip_html(meta.get("description") or ""),
        doi=doi,
        url=links.get("self_html") or (f"https://doi.org/{doi}" if doi else ""),
        item_type=_TYPE_MAP.get(kind, "document"),
        attachments=[pdf] if pdf else [],
        extra={"resource_type": kind,
               "license": (meta.get("license") or {}).get("id", ""),
               "keywords": ", ".join(meta.get("keywords") or []),
               "access_right": meta.get("access_right", "")},
    )


class ZenodoBackend(HttpBackend):
    """Read-only search over openly licensed records (datasets, software, preprints, reports)."""

    access = "oa"

    headers: ClassVar[dict] = {"Accept": "application/json"}

    def __init__(self, name: str = "zenodo", base: str = _API, timeout: int = 30, delay: float = 1.0):
        super().__init__(name=name, timeout=timeout, rate=1, min_interval=delay)
        self._base = base.rstrip("/")

    @property
    def capabilities(self) -> set[str]:
        # ``attachment`` off: the file URL is remote, not a local-store copy (same as scihub).
        return {"read"}


    def search(self, query: str, limit: int = 10, filters: dict | None = None) -> list[AcademicItem]:
        filters = self._filters(filters)
        parts = [f'title:"{query}"' if query.strip() else ""]
        if filters.get("title"):
            parts.append(f'title:"{filters["title"]}"')
        if filters.get("author"):
            parts.append(f'creators.name:"{filters["author"]}"')
        if filters.get("container"):
            parts.append(f'"{filters["container"]}"')
        q = " AND ".join(p for p in parts if p) or "*"
        year = filters.get("year") or ""
        if year.isdigit():
            # Filter in the query: a post-filter over the first page returns false empties.
            q += f" AND publication_date:[{year}-01-01 TO {year}-12-31]"
        # Anonymous search rejects size > 25 with HTTP 400, so page in size-25 batches.
        limit = max(1, limit)
        size = min(limit, 25)
        hits: list[dict] = []
        for page in range(1, (limit + size - 1) // size + 1):
            params = urllib.parse.urlencode({"q": q, "size": size, "page": page,
                                            "sort": "bestmatch"})
            batch = (self.fetch_json(f"{self._base}?{params}").get("hits") or {}).get("hits") or []
            hits.extend(batch)
            if len(batch) < size or len(hits) >= limit:
                break
        items = [record_to_item(h, self.name) for h in hits]
        if year:  # the range query is inclusive; keep the exact-year guarantee
            items = [i for i in items if i.year == year]
        return items[:limit]

    def get(self, key: str) -> AcademicItem:
        k = (key or "").strip()
        if k.startswith("zenodo/"):
            k = k[len("zenodo/"):]
        record_id = ""
        if k.isdigit():
            record_id = k
        else:
            m = _URL_ID_RE.search(k) or _DOI_ID_RE.search(k.replace("https://doi.org/", ""))
            if m:
                record_id = m.group(1)
        if record_id:
            record = self.fetch_json(f"{self._base}/{record_id}")
            if not record.get("id"):
                raise KeyError(f"[{self.name}] not found: {key}")
            return record_to_item(record, self.name)
        if k.lower().startswith("10."):
            hits = (self.fetch_json(f"{self._base}?q=doi:%22{urllib.parse.quote(k, safe='')}%22&size=1")
                    .get("hits") or {}).get("hits") or []
            if hits:
                return record_to_item(hits[0], self.name)
        raise KeyError(f"[{self.name}] not found: {key}")
