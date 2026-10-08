"""OpenAlex 后端:开放学术图谱(api.openalex.org),全球论文/图书/专利元数据。

只读。搜索(works?search=)或按 DOI(works/doi:...)。abstract 以倒排索引给出,需重建;
best_oa_location 提供**合法开放获取(OA)PDF 直链**,作为 attend 不入 Sci-Hub 的合规全文来源。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

from ..models import AcademicItem
from .base import Backend

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
    """把 OpenAlex 的倒排索引 {word:[pos]} 重建为可读句子(近似)。"""
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


def _work_to_item(rec: dict, source: str = "openalex") -> AcademicItem:
    doi = rec.get("doi", "") or ""
    title = rec.get("display_name", "") or ""
    return AcademicItem(
        source=source,
        key=doi,
        title=title,
        creators=_creators(rec),
        year=str(rec.get("publication_year") or ""),
        abstract=_inverted_to_text(rec.get("abstract_inverted_index")),
        doi=doi,
        url=doi or "",
        item_type=_TYPE_MAP.get(rec.get("type", ""), "document"),
        attachments=_oa_pdf(rec),
    )


class OpenAlexBackend(Backend):
    def __init__(self, name: str = "openalex", mailto: str = "",
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
            "User-Agent": f"litcli/0.1 (mailto: {self._mailto or 'unknown'})",
            "Accept": "application/json",
        }

    def _get(self, url: str) -> dict:
        """GET 并解析 JSON;对 5xx/网络错误做有限指数退避重试。"""
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
        params = urllib.parse.urlencode({"search": query, "per_page": limit})
        if self._mailto:
            params += "&mailto=" + urllib.parse.quote(self._mailto)
        data = self._get(f"{self._base}?{params}")
        return [_work_to_item(w, self.name) for w in (data.get("results") or [])]

    def get(self, key: str) -> AcademicItem:
        # key 可为 DOI(10... 或 https://doi.org/...)或 OpenAlex id
        if key.startswith("https://doi.org/"):
            key = key.split("doi.org/")[-1]
        if key.startswith("10."):
            url = f"{self._base}/doi:{urllib.parse.quote(key, safe='')}"
        else:
            url = f"{self._base}/{urllib.parse.quote(key, safe='')}"
        data = self._get(url)
        if not data:
            raise KeyError(f"[{self.name}] 未找到 {key}")
        return _work_to_item(data, self.name)
