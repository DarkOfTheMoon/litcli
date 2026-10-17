"""Backend abstract base class with capability annotations.

Multiple backends must handle differing capabilities: Zotero is read/write, arXiv is
read-only. A ``capabilities`` set (read / write / attachment) captures this, and the CLI
checks ``can("write")`` before running add/patch; read-only backends such as arXiv raise
NotSupportedError.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from abc import ABC, abstractmethod

from ..models import AcademicItem


def retry_json(url: str, headers: dict, name: str, timeout: int = 25,
               attempts: int = 3) -> dict:
    """GET ``url`` and parse JSON, retrying with exponential backoff on 5xx / network errors.

    Shared by the HTTP read-only backends (crossref/openalex/openlibrary); each caller keeps its
    own ``timeout`` and ``name`` (for the error prefix). Raises ``RuntimeError`` after the final
    attempt. Mirrors the original per-backend behavior exactly, just deduplicated.
    """
    req = urllib.request.Request(url, headers=headers)
    last: RuntimeError | None = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            err = RuntimeError(f"[{name}] HTTP {e.code}: {e.reason}")
            if e.code >= 500 and attempt < attempts - 1:
                time.sleep(0.5 * (attempt + 1))
                last = err
                continue
            raise err from e
        except urllib.error.URLError as e:
            err = RuntimeError(f"[{name}] unreachable: {e.reason}")
            if attempt < attempts - 1:
                time.sleep(0.5 * (attempt + 1))
                last = err
                continue
            raise err from e
    raise last  # pragma: no cover - already raised inside the loop


class NotSupportedError(NotImplementedError):
    """Raised when an unsupported operation is attempted on a given backend."""

    def __init__(self, op: str, backend: "Backend"):
        super().__init__(f"[{backend.name}] does not support operation {op} (capabilities={sorted(backend.capabilities)})")


class Backend(ABC):
    """Abstract base class for all academic backends."""

    name: str = ""

    @property
    def capabilities(self) -> set[str]:
        """Capability set; subclasses override. Common values: read / write / attachment."""
        return {"read"}

    def can(self, cap: str) -> bool:
        return cap in self.capabilities

    def _require(self, cap: str) -> None:
        if not self.can(cap):
            raise NotSupportedError(cap, self)

    @staticmethod
    def _filters(filters: dict | None) -> dict:
        """Coerce field-filter values to str so ``int`` years don't crash ``.isdigit()``;
        drop None/empty. Returns a fresh dict, never mutates the caller's input.
        Every backend ``search`` normalizes through this to keep a consistent contract
        ("map or fold into free-text, or ignore").
        """
        out: dict = {}
        for k, v in (filters or {}).items():
            if v is None:
                continue
            s = str(v)
            if s:
                out[k] = s
        return out

    @abstractmethod
    def search(self, query: str, limit: int = 10, filters: dict | None = None) -> list[AcademicItem]:
        """Search ``query`` and return normalized items.

        ``filters``: optional field-level predicates (AND-ed), keys are
        title/author/year/container, values str. A backend maps each to its own
        field query; unmappable fields are folded into free text or ignored.
        """

    @abstractmethod
    def get(self, key: str) -> AcademicItem:
        """Fetch a single item."""

    def list_collections(self) -> list[dict]:
        """List collections/folders (only backends that support it)."""
        raise NotSupportedError("list_collections", self)

    def collection_tree(self) -> list[dict]:
        """List full collection nodes with hierarchy (parent/child) (only backends that support it)."""
        raise NotSupportedError("collection_tree", self)

    def collection_items(self, coll_key: str, recursive: bool = True) -> list[AcademicItem]:
        """List items in a collection (optionally including all sub-collections) (only backends that support it)."""
        raise NotSupportedError("collection_items", self)

    def get_attachment(self, key: str) -> bytes:
        """Fetch the main PDF attachment bytes (only backends that support it)."""
        raise NotSupportedError("get_attachment", self)

    def related(self, key: str, limit: int = 5, sort_cited: bool = False) -> list[AcademicItem]:
        """Return works semantically related to ``key`` (citation-graph neighbors; openalex only)."""
        raise NotSupportedError("related", self)

    def references(self, key: str, limit: int = 5, sort_cited: bool = True) -> list[AcademicItem]:
        """Return the works ``key`` cites (backward bibliography, ranked by citation count; openalex only)."""
        raise NotSupportedError("references", self)

    def citing(self, key: str, limit: int = 5, sort_cited: bool = True) -> list[AcademicItem]:
        """Return the works that cite ``key`` (forward, ranked by citation count; openalex only)."""
        raise NotSupportedError("citing", self)

    def add(self, item: AcademicItem) -> str:
        """Add an item, returning the new item's key/identifier (only backends that carry 'write')."""
        self._require("write")
        raise NotImplementedError(f"[{self.name}] add not implemented")

    def patch(self, key: str, fields: dict) -> AcademicItem:
        """Update item fields (only backends that carry 'write')."""
        self._require("write")
        raise NotImplementedError(f"[{self.name}] patch not implemented")

    def delete(self, key: str) -> None:
        """Delete an item (only backends that carry 'write')."""
        self._require("write")
        raise NotImplementedError(f"[{self.name}] delete not implemented")

    def attach(self, key: str, src: str) -> str:
        """Attach a full-text attachment to an existing item ``key``, returning the attachment key
        (only backends that carry 'write').

        src: a local file path (file:// or absolute path) or a URL. Link mode references it, does
        not copy the content.
        """
        self._require("write")
        raise NotImplementedError(f"[{self.name}] attach not implemented")
