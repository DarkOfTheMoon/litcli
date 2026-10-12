"""USPTO 专利全文检索后端:美国专利商标局开放数据 API。

只读,key 门控(免费注册,https://developer.uspto.gov 顶部 Get a developer key)。
免 key 可达的开放元数据源(Crossref/OpenAlex)公开版专利记录趋近于 0,
故此后端是专利检索的主通道——但和 Zotero 写入一样,必须配置 api_key 才生效。

Endpoint: ``POST https://api.uspto.gov/api/v1/patent/search``
  Header:  ``X-Api-Key: <key>``
  Body:    ``{"query": {"fq": ["<search term>"]}}``  (fq = 全文 fielded query)

说明:字段名按 USPTO 官方文档实现;若真实响应字段名与文档不符,调整
``_pat_to_item`` 即可——本模块是「key 门控 + 可本地改字段」的专利入口,非依赖
某一固定 schema。未配置 key 时 ``search``/``get`` 抛 RuntimeError,CLI 优雅提示。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from ..models import AcademicItem
from .base import Backend

_API = "https://api.uspto.gov/api/v1/patent/search"

# 专利 → CSL/RIS 类型映射(base._bibtex_type/to_ris 已覆盖 patent)
_TYPE = "patent"


def _url(query: str) -> str:
    """专利在 Google Patents 的规范浏览页(元数据 URL,非下载直链)。"""
    return f"https://patents.google.com/patent/{query}"


def _pat_to_item(rec: dict, source: str = "uspto") -> AcademicItem:
    """把一条 USPTO patent 记录归一化为 AcademicItem(字段名按官方文档,缺失则优雅降级)。"""
    pat = rec or {}
    # USPTO 用 patent_patent_id / patent_title / patent_date / patent_abstract 等;
    # 兼容 PatentsView 风格别名(patent_id/patent_title/patent_date/patent_abstract)。
    pid = pat.get("patent_patent_id") or pat.get("patent_id") or ""
    title = pat.get("patent_title") or pat.get("title") or ""
    date = pat.get("patent_date") or pat.get("patent_date_issued") or ""
    abstract = pat.get("patent_abstract") or pat.get("abstract") or ""
    inventors: list[dict] = []
    inv = pat.get("patent_inventor") or pat.get("inventors") or pat.get("inventor") or []
    for a in inv if isinstance(inv, list) else []:
        if isinstance(a, dict):
            given = a.get("patent_inventor_first_name") or a.get("inventor_name_first") or a.get("first") or ""
            family = a.get("patent_inventor_last_name") or a.get("inventor_name_last") or a.get("last") or ""
        else:
            family, given = str(a), ""
        if family or given:
            inventors.append({"family": family, "given": given})
    year = str(date)[:4] if date else ""
    return AcademicItem(
        source=source,
        key=pid,
        title=title,
        creators=inventors,
        year=year,
        abstract=abstract,
        url=_url(pid) if pid else "",
        item_type=_TYPE,
    )


class UsptoBackend(Backend):
    """USPTO 专利检索后端(只读,key 门控)。"""

    def __init__(self, name: str = "uspto", api_key: str = "",
                 base: str = _API, max_results: int = 10):
        self.name = name
        self._api_key = api_key
        self._base = base.rstrip("/")
        self._max = max_results

    @property
    def capabilities(self) -> set[str]:
        return {"read"}

    def _require_key(self) -> None:
        if not self._api_key:
            raise RuntimeError(
                f"[{self.name}] 未配置 api_key:USPTO 专利检索需免费注册 key,"
                "请到 https://developer.uspto.gov 生成并写入 config 的 api_key")

    def _post(self, url: str, body: dict) -> dict:
        req = urllib.request.Request(
            url, method="POST",
            data=json.dumps(body).encode(),
            headers={"User-Agent": f"litcli/0.1 ({self.name})",
                     "Accept": "application/json",
                     "Content-Type": "application/json",
                     "X-Api-Key": self._api_key})
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            hint = "检查 api_key(403/401 多为 key 无效或未授权)" if e.code in (401, 403) else ""
            raise RuntimeError(f"[{self.name}] HTTP {e.code}: {e.reason} {hint}".strip()) from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"[{self.name}] 无法访问: {e.reason}") from e

    def _results(self, data: dict) -> list[dict]:
        """从响应中提取 results 数组;容忍 =None 等缺字段。"""
        if not isinstance(data, dict):
            return []
        res = data.get("results")
        if isinstance(res, list):
            return res
        if isinstance(data.get("patent"), dict) and isinstance(data["patent"].get("results"), list):
            return data["patent"]["results"]
        return []

    def search(self, query: str, limit: int = 10, filters: dict | None = None) -> list[AcademicItem]:
        # Patent full-text search only uses ``fq``; field filters are ignored (the patent
        # identity is application number/title, not bibliographic fields).
        self._require_key()
        body = {"query": {"fq": [query]}, "start": 0, "limit": limit}
        data = self._post(f"{self._base}", body)
        return [_pat_to_item(r, self.name) for r in self._results(data)][:limit]

    def get(self, key: str) -> AcademicItem:
        # 专利号精确查询:用 fq 精确匹配专利号字段
        self._require_key()
        body = {"query": {"fq": [f"patent_id:{key}"]}, "start": 0, "limit": 1}
        data = self._post(f"{self._base}", body)
        rows = self._results(data)
        if not rows:
            raise KeyError(f"[{self.name}] 未找到 {key}")
        return _pat_to_item(rows[0], self.name)

    def get_attachment(self, key: str) -> bytes:
        # USPTO 官方全文 PDF 需带 key 经 print/download 端点;此处不承诺自动拉全文 PDF。
        raise RuntimeError(f"[{self.name}] {key} 暂不支持自动下载全文 PDF(USPTO 需独立授权)")
