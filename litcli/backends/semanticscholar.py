"""Semantic Scholar backend: the citation graph API (api.semanticscholar.org).

Read-only, no key. It is the closest stand-in for OpenAlex and answers all three axes of a
survey walk — ``references``, ``citing``, and ``related`` (its recommendations endpoint is the
semantic-neighbour answer). No key is needed; the shared unauthenticated pool is throttled
hard, so the fallback chain puts it after OpenAlex rather than before.

Papers are identified by DOI where the record has one (``DOI:<doi>``) or by the S2 paper id;
``get`` accepts a DOI, a paper id, or a paper URL. Papers without a DOI keep their S2 id as
the key, which is what a walk can still be asked to expand.
"""
from __future__ import annotations

import re
import urllib.parse
from typing import ClassVar

from ..errors import NotFound
from ..models import AcademicItem, normalize_author_name
from ..transport import HttpError
from .http import HttpBackend

_API = "https://api.semanticscholar.org/graph/v1"
_REC = "https://api.semanticscholar.org/recommendations/v1"
_PAPER_FIELDS = "paperId,title,abstract,year,venue,citationCount,externalIds,openAccessPdf,authors"
_URL_RE = re.compile(r"semanticscholar\.org/paper/(?:[^/]+/)?([0-9a-f]{40})", re.I)
_ID_RE = re.compile(r"^(?:s2:|semanticscholar:)?([0-9a-f]{40})$", re.I)


def _identifier(key: str) -> str:
    """The path segment for a key: ``DOI:<doi>``, a bare paper id, or the key as given."""
    k = (key or "").strip()
    m = _URL_RE.search(k)
    if m:
        return m.group(1)
    m = _ID_RE.match(k)
    if m:
        return m.group(1)
    for prefix in ("doi:", "DOI:", "https://doi.org/", "s2:", "semanticscholar:"):
        if k.startswith(prefix):
            k = k[len(prefix):]
            break
    if k.startswith("10."):
        return "DOI:" + urllib.parse.quote(k, safe="/:;()._-")
    return urllib.parse.quote(k, safe="/:;()._-")


def _paper(rec: dict, source: str) -> AcademicItem:
    ids = rec.get("externalIds") or {}
    doi = ids.get("DOI") or ""
    pdf = (rec.get("openAccessPdf") or {}).get("url") or ""
    return AcademicItem(
        source=source,
        key=doi or str(rec.get("paperId") or ""),
        title=rec.get("title") or "",
        creators=[normalize_author_name((a or {}).get("name") or "") for a in
                  rec.get("authors") or []],
        year=str(rec.get("year") or ""),
        abstract=rec.get("abstract") or "",
        doi=doi,
        url=id_url(rec),
        container_title=rec.get("venue") or "",
        cited_by_count=rec.get("citationCount") or 0,
        item_type="article",
        attachments=[pdf] if pdf else [],
    )


def id_url(rec: dict) -> str:
    """The public page of a record (DOI when there is one)."""
    doi = (rec.get("externalIds") or {}).get("DOI") or ""
    if doi:
        return f"https://doi.org/{doi}"
    return f"https://www.semanticscholar.org/paper/{rec.get('paperId') or ''}"


class SemanticScholarBackend(HttpBackend):
    name = "semanticscholar"
    headers: ClassVar[dict] = {"Accept": "application/json"}
    # The unauthenticated pool is shared by everyone; one request a second is the polite rate
    # the keyed tier gets anyway, and pacing below it only burns retries.
    min_interval = 0.3

    def __init__(self, name: str = "semanticscholar", mailto: str = "", api_key: str = "",
                 base: str = _API, max_results: int = 10, retries=None, retry_after_cap=None):
        super().__init__(name=name, retries=retries, retry_after_cap=retry_after_cap)
        self._base = base.rstrip("/")
        self._mailto = mailto
        self._max = max_results
        if api_key:
            # A key lifts the shared-pool throttling; S2 reads it as a request header.
            self.headers = {**self.headers, "x-api-key": api_key}
        self.UA = f"litcli/0.1 (contact: {mailto or 'none'})"

    @property
    def capabilities(self) -> set[str]:
        return {"read", "refs", "citing", "related"}

    def _params(self, limit: int, cap: int = 100, **extra) -> str:
        params = {"fields": _PAPER_FIELDS, "limit": min(max(int(limit), 1), cap), **extra}
        return urllib.parse.urlencode(params)

    def _edge(self, key: str, edge: str, slot: str, limit: int,
              sort_cited: bool) -> list[AcademicItem]:
        data = self.fetch_json(f"{self._base}/paper/{_identifier(key)}/{edge}?{self._params(limit)}")
        out = []
        for row in data.get("data") or []:
            rec = row.get(slot) or {}
            if rec:
                out.append(_paper(rec, self.name))
        if sort_cited:
            # S2 has no sort parameter on the edges; the bibliography contract is by citations.
            out.sort(key=lambda i: -int(i.cited_by_count or 0))
        return out[:limit]

    def search(self, query: str, limit: int = 10, filters: dict | None = None) -> list[AcademicItem]:
        params = {"query": query, "fields": _PAPER_FIELDS, "limit": min(max(limit, 1), self._max)}
        data = self.fetch_json(f"{self._base}/paper/search?{urllib.parse.urlencode(params)}")
        return [_paper(r, self.name) for r in data.get("data") or [] if r]

    def get(self, key: str) -> AcademicItem:
        try:
            rec = self.fetch_json(f"{self._base}/paper/{_identifier(key)}?{self._params(1)}")
        except HttpError as e:
            if e.status == 404:
                raise NotFound(f"[{self.name}] no such paper: {key}") from e
            raise
        if not rec or not rec.get("paperId"):
            raise NotFound(f"[{self.name}] no such paper: {key}")
        return _paper(rec, self.name)

    def references(self, key: str, limit: int = 5, sort_cited: bool = True) -> list[AcademicItem]:
        return self._edge(key, "references", "citedPaper", limit, sort_cited)

    def citing(self, key: str, limit: int = 5, sort_cited: bool = True) -> list[AcademicItem]:
        return self._edge(key, "citations", "citingPaper", limit, sort_cited)

    def related(self, key: str, limit: int = 5, sort_cited: bool = False) -> list[AcademicItem]:
        """Semantic neighbours: S2 recommendations, without the seed itself."""
        data = self.fetch_json(
            f"{_REC}/papers/forpaper/{_identifier(key)}?{self._params(limit + 1)}")
        mine = _identifier(key)
        # The path segment is percent-quoted; the API answers the raw spelling, so the seed's
        # DOI is unquoted before it is compared or every quoted DOI (``<``, ``>``, ``;``…)
        # stays in its own recommendation list.
        mine_doi = urllib.parse.unquote(mine[len("DOI:"):]) if mine.startswith("DOI:") else ""
        out = []
        for rec in data.get("recommendedPapers") or []:
            if not rec or str(rec.get("paperId")) == mine:
                continue
            if mine_doi and (rec.get("externalIds") or {}).get("DOI") == mine_doi:
                continue
            out.append(_paper(rec, self.name))
        return out[:limit]
