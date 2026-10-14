"""Backend 抽象基类 + 能力标注(capabilities)。

多后端必须处理「各后端能力不同」:Zotero 可读可写、arXiv 只读。
执象用一个 capabilities 集合(读=read / 写=write / 附附件=attachment),
CLI 在执行 add/patch 前先查 can("write"),arXiv 这类只读后端将抛 NotSupportedError。
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import AcademicItem


class NotSupportedError(NotImplementedError):
    """对指定后端发起其不支持的操作用。"""

    def __init__(self, op: str, backend: "Backend"):
        super().__init__(f"[{backend.name}] 不支持操作 {op} (capabilities={sorted(backend.capabilities)})")


class Backend(ABC):
    """所有学术后端的抽象基类。"""

    name: str = ""

    @property
    def capabilities(self) -> set[str]:
        """能力集,子类覆盖。常用: read / write / attachment。"""
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
        """取单个条目。"""

    def list_collections(self) -> list[dict]:
        """列出集合/分类(仅支持的后端)。"""
        raise NotSupportedError("list_collections", self)

    def collection_tree(self) -> list[dict]:
        """列出含层级(父/子)的完整集合节点(仅支持的后端)。"""
        raise NotSupportedError("collection_tree", self)

    def collection_items(self, coll_key: str, recursive: bool = True) -> list[AcademicItem]:
        """列出某集合(可选含全部子集合)的条目(仅支持的后端)。"""
        raise NotSupportedError("collection_items", self)

    def get_attachment(self, key: str) -> bytes:
        """取主 PDF 附件字节(仅支持的后端)。"""
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
        """新增条目,返回新条目的 key/标识(仅 carries 'write' 的后端)。"""
        self._require("write")
        raise NotImplementedError(f"[{self.name}] add 未实现")

    def patch(self, key: str, fields: dict) -> AcademicItem:
        """修改条目字段(仅 carries 'write' 的后端)。"""
        self._require("write")
        raise NotImplementedError(f"[{self.name}] patch 未实现")

    def delete(self, key: str) -> None:
        """删除条目(仅 carries 'write' 的后端)。"""
        self._require("write")
        raise NotImplementedError(f"[{self.name}] delete 未实现")

    def attach(self, key: str, src: str) -> str:
        """给已有条目 key 挂全文附件,返回附件 key(仅 carries 'write' 的后端)。

        src: 本地文件路径(file:// 或绝对路径)或 URL。link 模式引用,不复制内容。
        """
        self._require("write")
        raise NotImplementedError(f"[{self.name}] attach 未实现")
