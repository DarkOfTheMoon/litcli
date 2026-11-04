"""OpenLibrary book backend: openlibrary.org open book metadata (books/covers/previews).

Read-only. search.json searches by title/author, or fetches a single record by a /works/OL /
/books/OL key. Covers the "book" class of metadata sources.
"""
from __future__ import annotations

import urllib.parse
from typing import ClassVar

from ..models import AcademicItem
from .http import HttpBackend

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


class OpenLibraryBackend(HttpBackend):
    UA = "litcli/0.1 (library research)"
    headers: ClassVar[dict] = {"Accept": "application/json"}
    timeout = 20.0

    def __init__(self, name: str = "openlibrary", base: str = _SEARCH, max_results: int = 10):
        super().__init__(name=name, rate=5, min_interval=0.2)
        self._base = base.rstrip("/")
        self._max = max_results

    @property
    def capabilities(self) -> set[str]:
        return {"read"}


    def search(self, query: str, limit: int = 10, filters: dict | None = None) -> list[AcademicItem]:
        # Book search is a single ``q``; field words fold into free-text (secondary backend).
        filters = self._filters(filters)
        terms = [t for t in (query, filters.get("title"),
                             filters.get("author"),
                             filters.get("container")) if t]
        params = urllib.parse.urlencode({"q": " ".join(terms), "limit": limit})
        data = self.fetch_json(f"{self._base}?{params}")
        return [_doc_to_item(d, self.name) for d in (data.get("docs") or [])]

    def get(self, key: str) -> AcademicItem:
        # key looks like /works/OL... /books/OL...; use the respective .json endpoint to fetch a single record
        ident = key.rstrip("/").split("/")[-1]
        kind = "books" if key.startswith("/books/") else "works"
        url = f"{_SITE}/{kind}/{urllib.parse.quote(ident, safe='')}.json"
        data = self.fetch_json(url)
        if not data:
            raise KeyError(f"[{self.name}] not found: {key}")
        return _doc_to_item(data | {"key": key}, self.name)
