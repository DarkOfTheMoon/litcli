"""arXiv backend: through its open API (http://export.arxiv.org/api/query), Atom/XML.

A pure network backend, **read-only** (capabilities has no write, but can pull PDF direct links ->
attachment). Items are normalized to AcademicItem by arXiv id (e.g. 2006.12345).
"""
from __future__ import annotations

import re
import urllib.parse

from lxml import etree as ET

from .. import pdf as pdfutil
from ..errors import Unavailable
from ..models import AcademicItem
from .http import HttpBackend, HttpError

_NS = {"a": "http://www.w3.org/2005/Atom",
       "arxiv": "http://arxiv.org/schemas/atom"}

_API = "https://export.arxiv.org/api/query"
_PDF = "https://arxiv.org/pdf"

# arXiv's API rate-limits per IP and (2023+) expects a descriptive user-agent; a bare/generic
# UA or a burst can get a 429 "Rate exceeded."  We retry throttle/5xx with backoff and say so.


def _short_id(eid: str) -> str:
    """'http://arxiv.org/abs/2006.12345v1' -> '2006.12345' (strips the version suffix)"""
    tail = eid.rstrip("/").split("/")[-1]
    return re.sub(r"v\d+$", "", tail)


def _creator_from_name(name: str) -> dict:
    name = (name or "").strip()
    if not name:
        return {"family": "", "given": ""}
    tokens = name.split()
    if len(tokens) == 1:
        return {"family": tokens[0], "given": ""}
    return {"family": tokens[-1], "given": " ".join(tokens[:-1])}


def _entry_to_item(e: ET.Element) -> AcademicItem:
    eid = e.findtext("a:id", default="", namespaces=_NS)
    title = " ".join((e.findtext("a:title", default="", namespaces=_NS) or "").split())
    summary = " ".join((e.findtext("a:summary", default="", namespaces=_NS) or "").split())
    published = e.findtext("a:published", default="", namespaces=_NS)
    authors = [_creator_from_name(n) for n in
               (a.findtext("a:name", default="", namespaces=_NS) for a in e.findall("a:author", _NS))]
    doi = e.findtext("arxiv:doi", default="", namespaces=_NS)
    return AcademicItem(
        source="arxiv",
        key=_short_id(eid),
        title=title,
        creators=[a for a in authors if a["family"] or a["given"]],
        year=published[:4] if published else "",
        abstract=summary,
        doi=doi,
        url=eid,
        item_type="article",
        extra={"published": published},
    )


class ArxivBackend(HttpBackend):
    timeout = 25.0
    rate = 1
    min_interval = 3.0       # arXiv asks for ~1 request / 3 s

    def __init__(self, name: str = "arxiv", max_results: int = 10, mailto: str = ""):
        super().__init__(name=name)
        self._max = max_results
        self._mailto = mailto
        self.UA = self._ua()

    @property
    def capabilities(self) -> set[str]:
        return {"read", "attachment"}  # read-only, but can pull PDF direct links

    def _ua(self) -> str:
        """A descriptive user-agent (arXiv expects tool + contact); mailto helps polite-pool pacing."""
        contact = f"; mailto:{self._mailto}" if self._mailto else ""
        return f"litcli/0.1 (academic search CLI{contact})"

    def _query(self, search_query: str, limit: int) -> list[AcademicItem]:
        params = urllib.parse.urlencode({"search_query": search_query, "max_results": limit})
        try:
            xml = self.fetch(f"{_API}?{params}",
                             headers={"Accept": "application/atom+xml"}).content
        except HttpError as e:
            if e.status == 429:  # throttled: say so instead of the bare status
                raise Unavailable(
                    f"[{self.name}] arXiv rate-limited (HTTP 429): the API is throttling this "
                    f"network; wait a moment and retry, or set a mailto in [backends.arxiv] to "
                    f"join the polite pool") from e
            raise
        root = ET.fromstring(xml)
        return [_entry_to_item(e) for e in root.findall("a:entry", _NS)]

    def search(self, query: str, limit: int = 10, filters: dict | None = None) -> list[AcademicItem]:
        # all: free-text (title/abstract/author); left unquoted so arXiv does token AND,
        # avoiding a natural user phrase being treated as an exact string (0 hits).
        # Field filters stack with AND per arXiv syntax. ti:/au: values are quoted so a
        # multi-word/comma value (e.g. "Droniou, J.") stays one term — unquoted it becomes
        # a bare OR over the whole corpus.
        filt = self._filters(filters)
        parts = []
        if query:
            parts.append(f"all:{query}")
        if filt.get("title"):
            parts.append(f'ti:"{filt["title"]}"')
        if filt.get("author"):
            parts.append(f'au:"{filt["author"]}"')
        if filt.get("year") and filt["year"].isdigit():
            y = filt["year"]
            parts.append(f"submittedDate:[{y}01010000 TO {y}12312359]")
        q = " AND ".join(parts) if parts else f"all:{query}"
        return self._query(q, limit)

    def get(self, key: str) -> AcademicItem:
        items = self._query(f'id:{key}', 1)
        if not items:
            raise KeyError(f"[{self.name}] arXiv not found: {key}")
        return items[0]

    def get_attachment(self, key: str) -> bytes:
        return pdfutil.download(f"{_PDF}/{key}")
