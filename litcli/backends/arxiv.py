"""arXiv 后端:走其开放 API(http://export.arxiv.org/api/query),Atom/XML。

纯网络后端,**只读**(capabilities 无 write,但可拉 PDF 直链 -> attachment).
条目按 arXiv id(如 2006.12345)归一化到 AcademicItem。
"""
from __future__ import annotations

import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from ..models import AcademicItem
from .. import pdf as pdfutil
from .base import Backend

_NS = {"a": "http://www.w3.org/2005/Atom",
       "arxiv": "http://arxiv.org/schemas/atom"}

_API = "https://export.arxiv.org/api/query"
_PDF = "https://arxiv.org/pdf"


def _short_id(eid: str) -> str:
    """'http://arxiv.org/abs/2006.12345v1' -> '2006.12345'(去版本)"""
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


class ArxivBackend(Backend):
    def __init__(self, name: str = "arxiv", max_results: int = 10):
        self.name = name
        self._max = max_results

    @property
    def capabilities(self) -> set[str]:
        return {"read", "attachment"}  # 只读,但可拉 PDF 直链

    def _query(self, search_query: str, limit: int) -> list[AcademicItem]:
        params = urllib.parse.urlencode({"search_query": search_query, "max_results": limit})
        url = f"{_API}?{params}"
        req = urllib.request.Request(url, headers={
            "User-Agent": "litcli/0.1 (research protocol)",
            "Accept": "application/atom+xml",
        })
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                xml = resp.read()
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"[{self.name}] arXiv 请求失败 HTTP {e.code}: {e.reason}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"[{self.name}] arXiv 无法访问: {e.reason}") from e
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
            raise KeyError(f"[{self.name}] arXiv 未找到 {key}")
        return items[0]

    def get_attachment(self, key: str) -> bytes:
        return pdfutil.download(f"{_PDF}/{key}")
