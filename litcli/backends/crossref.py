"""Crossref 后端:权威开源元数据 API(api.crossref.org)。

覆盖论文/图书/会议录/专利等各类 DOI 登记元数据;只读。无需 key(带 mailto 可获更好配额)。
按标题检索,或凭 DOI 直接取单条。归一化到 AcademicItem。
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from ..models import AcademicItem
from .base import Backend

_API = "https://api.crossref.org/works"

# Crossref type -> AcademicItem.item_type(CSL 类型)
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
    """去 JATS/HTML 标签(Crossref abstract 常含)。"""
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
    return AcademicItem(
        source=source,
        key=rec.get("DOI", ""),
        title=title,
        creators=_authors(rec),
        year=year,
        abstract=_strip_xml(rec.get("abstract", "")),
        doi=rec.get("DOI", ""),
        url=rec.get("URL", ""),
        item_type=_TYPE_MAP.get(rec.get("type", ""), "document"),
        attachments=_pdf_links(rec),
    )


class CrossrefBackend(Backend):
    def __init__(self, name: str = "crossref", mailto: str = "",
                 base: str = _API, max_results: int = 10):
        self.name = name
        self._base = base.rstrip("/")
        self._mailto = mailto
        self._max = max_results

    @property
    def capabilities(self) -> set[str]:
        return {"read"}

    def _headers(self) -> dict:
        return {
            "User-Agent": f"litcli/0.1 (contact: {self._mailto or 'none'})",
            "Accept": "application/json",
        }

    def _get(self, url: str) -> dict:
        """GET 并解析 JSON;对 5xx/网络错误做有限指数退避重试(Crossref 偶发 5xx)。"""
        req = urllib.request.Request(url, headers=self._headers())
        last: RuntimeError | None = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=25) as r:
                    return json.loads(r.read())
            except urllib.error.HTTPError as e:
                err = RuntimeError(f"[{self.name}] HTTP {e.code}: {e.reason}")
                if e.code >= 500 and attempt < 2:
                    time.sleep(0.5 * (attempt + 1))
                    last = err
                    continue
                raise err from e
            except urllib.error.URLError as e:
                err = RuntimeError(f"[{self.name}] 无法访问: {e.reason}")
                if attempt < 2:
                    time.sleep(0.5 * (attempt + 1))
                    last = err
                    continue
                raise err from e
        raise last  # pragma: no cover - 循环内已 raise

    def search(self, query: str, limit: int = 10) -> list[AcademicItem]:
        params = urllib.parse.urlencode({"query.bibliographic": query, "rows": limit})
        data = self._get(f"{self._base}?{params}")
        items = data.get("message", {}).get("items") or []
        return [_item_to_academic(i, self.name) for i in items]

    def get(self, key: str) -> AcademicItem:
        doi = urllib.parse.quote(key, safe="")
        data = self._get(f"{self._base}/{doi}")
        msg = data.get("message")
        if not msg:
            raise KeyError(f"[{self.name}] 未找到 {key}")
        return _item_to_academic(msg, self.name)
