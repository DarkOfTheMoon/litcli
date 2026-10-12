"""OpenLibrary 图书后端:openlibrary.org 开放图书元数据(书籍/封面/预览)。

只读。search.json 按题名/作者检索,或按 /works/OL / /books/OL key 取单条。
覆盖「图书」这一类元数据来源。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

from ..models import AcademicItem
from .base import Backend

_SEARCH = "https://openlibrary.org/search.json"
_SITE = "https://openlibrary.org"


def _creators(names: list[str]) -> list[dict]:
    out = []
    for n in names or []:
        if not n:
            continue
        parts = n.split()
        if len(parts) > 1:
            out.append({"family": parts[-1], "given": " ".join(parts[:-1])})
        else:
            out.append({"family": n, "given": ""})
    return out


def _doc_to_item(doc: dict, source: str = "openlibrary") -> AcademicItem:
    key = doc.get("key", "")
    url = f"{_SITE}{key}" if key.startswith("/") else key
    return AcademicItem(
        source=source,
        key=key,
        title=doc.get("title") or "",
        creators=_creators(doc.get("author_name")),
        year=str(doc.get("first_publish_year") or ""),
        abstract="",
        doi="",
        url=url,
        item_type="book",
        extra={"cover_i": doc.get("cover_i", "")},
    )


class OpenLibraryBackend(Backend):
    def __init__(self, name: str = "openlibrary", base: str = _SEARCH, max_results: int = 10):
        self.name = name
        self._base = base.rstrip("/")
        self._max = max_results

    @property
    def capabilities(self) -> set[str]:
        return {"read"}

    def _headers(self) -> dict:
        return {"User-Agent": "litcli/0.1 (library research)", "Accept": "application/json"}

    def _get(self, url: str) -> dict:
        req = urllib.request.Request(url, headers=self._headers())
        last: RuntimeError | None = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
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

    def search(self, query: str, limit: int = 10, filters: dict | None = None) -> list[AcademicItem]:
        # Book search is a single ``q``; field words fold into free-text (secondary backend).
        filters = self._filters(filters)
        terms = [t for t in (query, filters.get("title"),
                             filters.get("author"),
                             filters.get("container")) if t]
        params = urllib.parse.urlencode({"q": " ".join(terms), "limit": limit})
        data = self._get(f"{self._base}?{params}")
        return [_doc_to_item(d, self.name) for d in (data.get("docs") or [])]

    def get(self, key: str) -> AcademicItem:
        # key 形如 /works/OL... /books/OL...;用各自 .json 端点取单条
        ident = key.rstrip("/").split("/")[-1]
        kind = "books" if key.startswith("/books/") else "works"
        url = f"{_SITE}/{kind}/{urllib.parse.quote(ident, safe='')}.json"
        data = self._get(url)
        if not data:
            raise KeyError(f"[{self.name}] 未找到 {key}")
        return _doc_to_item(data | {"key": key}, self.name)
