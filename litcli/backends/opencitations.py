"""OpenCitations (COCI) backend: CC0 citation links, DOI in, DOI out.

Read-only, no key, no metadata. COCI is built from Crossref's deposited reference lists, so
each row is a citing/cited DOI pair — useful exactly where a metadata service is throttled or
down, which is why it sits on the fallback path of both directions. It answers ``references``
and ``citing`` and nothing else; the DOI it hands back is hydrated elsewhere before it can be
scored, and ``get`` therefore returns the bare key.

The fulltext chain is unaffected: this backend never carries an attachment.
"""
from __future__ import annotations

import urllib.parse
from typing import ClassVar

from ..models import AcademicItem
from .http import HttpBackend

_API = "https://opencitations.net/index/coci/api/v1"
_SAFE = "/:;()._"


class OpenCitationsBackend(HttpBackend):
    name = "opencitations"
    headers: ClassVar[dict] = {"Accept": "application/json"}
    min_interval = 0.2

    def __init__(self, name: str = "opencitations", base: str = _API, max_results: int = 50,
                 retries=None, retry_after_cap=None):
        super().__init__(name=name, retries=retries, retry_after_cap=retry_after_cap)
        self._base = base.rstrip("/")
        self._max = max_results

    @property
    def capabilities(self) -> set[str]:
        return {"read", "refs", "citing"}

    def _edge(self, key: str, edge: str, column: str, limit: int) -> list[AcademicItem]:
        doi = (key or "").strip()
        for prefix in ("doi:", "DOI:", "https://doi.org/", "opencitations:"):
            if doi.startswith(prefix):
                doi = doi[len(prefix):]
                break
        rows = self.fetch_json(f"{self._base}/{edge}/{urllib.parse.quote(doi, safe=_SAFE)}")
        out = []
        for row in rows or []:
            target = (row or {}).get(column) or ""
            if target:
                out.append(AcademicItem(source=self.name, key=target, doi=target))
            if len(out) >= limit:
                break
        return out

    def search(self, query: str, limit: int = 10, filters: dict | None = None) -> list[AcademicItem]:
        """COCI is a link service: only a DOI-shaped query can be answered."""
        q = (query or "").strip()
        if q.startswith("10."):
            return [self.get(q)] if self.get(q).doi else []
        return []

    def get(self, key: str) -> AcademicItem:
        doi = (key or "").strip()
        for prefix in ("doi:", "DOI:", "https://doi.org/", "opencitations:"):
            if doi.startswith(prefix):
                doi = doi[len(prefix):]
                break
        return AcademicItem(source=self.name, key=doi, doi=doi,
                            url=f"https://doi.org/{doi}" if doi else "")

    def references(self, key: str, limit: int = 5, sort_cited: bool = True) -> list[AcademicItem]:
        return self._edge(key, "references", "cited", limit)

    def citing(self, key: str, limit: int = 5, sort_cited: bool = True) -> list[AcademicItem]:
        return self._edge(key, "citations", "citing", limit)
