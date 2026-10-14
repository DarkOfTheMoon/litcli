"""OpenAlex 后端:开放学术图谱(api.openalex.org),全球论文/图书/专利元数据。

只读。搜索(works?search=)或按 DOI(works/doi:...)。abstract 以倒排索引给出,需重建;
best_oa_location 提供**合法开放获取(OA)PDF 直链**,作为 attend 不入 Sci-Hub 的合规全文来源。
"""
from __future__ import annotations

import concurrent.futures
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from ..aggregate import _cited_key
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


def _bare_doi(value: str) -> str:
    """OpenAlex 的 doi 是完整 URL(https://doi.org/10.x);剥成裸 DOI 供身份键/导出用。"""
    d = (value or "").strip()
    for prefix in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/"):
        if d.lower().startswith(prefix):
            return d[len(prefix):]
    return d


def _wid(value: str) -> str:
    """Extract the trailing W-id from an OpenAlex id (``https://openalex.org/W...`` or bare ``W...``)."""
    return (value or "").rstrip("/").split("/")[-1]


def _journal_name(rec: dict) -> str:
    src = (rec.get("primary_location") or {}).get("source") or {}
    return src.get("display_name", "") or ""


def _biblio(rec: dict) -> tuple[str, str, str]:
    """OpenAlex biblio 对象 -> (volume, issue, page);page 用 first_page-last_page 拼接。"""
    b = rec.get("biblio") or {}
    volume = b.get("volume") or ""
    issue = b.get("issue") or ""
    fp = b.get("first_page") or ""
    lp = b.get("last_page") or ""
    page = f"{fp}-{lp}" if fp and lp else fp
    return volume, issue, page


def _work_to_item(rec: dict, source: str = "openalex") -> AcademicItem:
    doi = _bare_doi(rec.get("doi") or "")
    title = rec.get("display_name", "") or ""
    volume, issue, page = _biblio(rec)
    return AcademicItem(
        source=source,
        key=doi,
        title=title,
        creators=_creators(rec),
        year=str(rec.get("publication_year") or ""),
        abstract=_inverted_to_text(rec.get("abstract_inverted_index")),
        doi=doi,
        url=doi or "",
        container_title=_journal_name(rec),
        volume=volume,
        issue=issue,
        page=page,
        publisher=rec.get("publisher", "") or "",
        cited_by_count=rec.get("cited_by_count") or 0,
        item_type=_TYPE_MAP.get(rec.get("type", ""), "document"),
        attachments=_oa_pdf(rec),
        # Keep the internal OpenAlex id (W...) so ``related`` can source inline related_works.
        extra={"openalex_id": rec.get("id", "")},
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

    def _with_mailto(self, url: str) -> str:
        """Append the polite ``&mailto=`` param (OpenAlex asks for it) to a URL."""
        if not self._mailto:
            return url
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}mailto={urllib.parse.quote(self._mailto)}"

    def _work_url(self, key: str) -> str:
        """Build a ``/works/...`` URL from a DOI, a bare W-id, or an openalex.org URL.

        Handles the exact key forms the CLI emits/serializes: ``https://openalex.org/W...``
        (extra["openalex_id"]), a bare ``W...`` id, or a DOI (``10...`` / ``https://doi.org/...``).
        DOIs legitimately contain ``/``, so only a domain-prefixed/openalex W-id collapses to
        its final path segment.
        """
        k = (key or "").strip()
        for prefix in ("https://openalex.org/", "http://openalex.org/",
                       "https://api.openalex.org/works/"):
            if k.startswith(prefix):
                k = k[len(prefix):]
                break
        if k.lower().startswith(("https://doi.org/", "http://doi.org/")):
            k = k.split("doi.org/", 1)[1]
        if k.startswith("W") and "/" in k:
            k = k.rsplit("/", 1)[-1]
        k = k.rstrip("/")
        if k.startswith("10."):
            return f"{self._base}/doi:{urllib.parse.quote(k, safe='')}"
        return f"{self._base}/{urllib.parse.quote(k, safe='')}"

    def _work(self, key: str) -> dict:
        """Fetch one raw work dict (single GET): DOI/W-id integration + key-miss guard."""
        data = self._get(self._with_mailto(self._work_url(key)))
        if not data:
            raise KeyError(f"[{self.name}] 未找到 {key}")
        return data

    def search(self, query: str, limit: int = 10, filters: dict | None = None) -> list[AcademicItem]:
        filters = self._filters(filters)
        # Free query + container term go into the free-text ``search`` (OpenAlex has no
        # hard container filter; ``primary_location.source.display_name.search`` 400s).
        terms = [t for t in (query, filters.get("container")) if t]
        params: dict = {"search": " ".join(terms), "per_page": limit}
        oa_filters = []
        # OpenAlex splits ``filter`` on commas, so a value containing one (e.g. author
        # "Bonaventura, J.") must be double-quoted or the whole query 400s.
        for field, key in (("title.search", "title"), ("raw_author_name.search", "author")):
            if filters.get(key):
                oa_filters.append(f'{field}:"{filters[key]}"')
        if filters.get("year") and filters["year"].isdigit():
            oa_filters.append(f"publication_year:{filters['year']}")
        if oa_filters:
            params["filter"] = ",".join(oa_filters)
        qs = urllib.parse.urlencode(params)
        data = self._get(self._with_mailto(f"{self._base}?{qs}"))
        return [_work_to_item(w, self.name) for w in (data.get("results") or [])]

    def _fetch_works(self, ids: list[str]) -> list[AcademicItem]:
        """Parallel-fetch W-ids into AcademicItems, preserving id order, skipping per-id failures."""
        ids = [i for i in ids if i]
        if not ids:
            return []
        results: dict[str, AcademicItem] = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(ids))) as ex:
            futures = {ex.submit(self._get, self._with_mailto(f"{self._base}/{urllib.parse.quote(i, safe='')}")): i
                       for i in ids}
            for fut in concurrent.futures.as_completed(futures):
                i = futures[fut]
                try:
                    results[i] = _work_to_item(fut.result(), self.name)
                except (RuntimeError, urllib.error.URLError):
                    continue
        return [results[i] for i in ids if i in results]

    def _inline_works(self, key: str, field: str, limit: int = 5,
                      sort: bool = False, budget: int | None = None) -> list[AcademicItem]:
        """Fetch a parent work's inline W-id list (``field``) in parallel into items.

        Preserves inline order and skips per-id fetch failures. ``sort`` ranks by citation
        count (importance). ``budget`` caps how many W-ids are fetched (default: all).
        """
        limit = max(0, limit)
        rec = self._work(key)
        ids = [_wid(r) for r in (rec.get(field) or [])]
        if budget is not None:
            ids = ids[:min(len(ids), budget)]
        items = self._fetch_works(ids)
        if sort:
            items.sort(key=_cited_key)
        return items[:limit]

    def related(self, key: str, limit: int = 5) -> list[AcademicItem]:
        """Return works semantically related to ``key`` (OpenAlex ``related_works``).

        Reads the parent work's inline ``related_works`` (a list of W-ids) and fetches
        each in parallel; ``/works/{id}/related`` returns empty here, so it is unused.
        Per-work fetch failures are skipped. Order follows the inline list.
        """
        return self._inline_works(key, "related_works", limit, budget=limit)

    def references(self, key: str, limit: int = 5) -> list[AcademicItem]:
        """Return the works ``key`` cites (backward bibliography: inline ``referenced_works``).

        The foundational works a paper builds on; fetched in parallel and ranked by
        citation count (importance) before truncating to ``limit``. A fetch budget of
        ``max(limit, 50)`` bounds work on reference-dense reviews.
        """
        return self._inline_works(key, "referenced_works", limit, sort=True, budget=max(limit, 50))

    def citing(self, key: str, limit: int = 5) -> list[AcademicItem]:
        """Return works that cite ``key`` (forward), via ``/works?filter=cites:<wid>``.

        The citing works arrive as full records in one request (no per-work fetch);
        sorted server-side by citation count so the most impactful follow-ups come first.
        """
        limit = max(0, limit)
        wid = _wid(key)
        if not wid.startswith("W"):
            # A DOI/arXiv key must resolve to a W-id via the parent record; a bare
            # W-id (the common case, from ``extra["openalex_id"]``) needs no GET.
            rec = self._work(key)
            wid = _wid(rec.get("id") or "")
        if not wid:
            raise RuntimeError(f"[{self.name}] {key} 解析不到 OpenAlex 内部 id")
        data = self._get(self._with_mailto(
            f"{self._base}?filter=cites:{wid}&per_page={limit}&sort=cited_by_count:desc"))
        return [_work_to_item(w, self.name) for w in (data.get("results") or [])][:limit]

    def get(self, key: str) -> AcademicItem:
        return _work_to_item(self._work(key), self.name)

    def get_attachment(self, key: str) -> bytes:
        """下载合法 OA PDF 直链(best_oa_location 的 pdf_url)。"""
        item = self.get(key)
        if not item.attachments:
            raise RuntimeError(f"[{self.name}] {key} 无 OA PDF 直链")
        from .. import pdf as pdfutil
        return pdfutil.download(item.attachments[0])
