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

    @abstractmethod
    def search(self, query: str, limit: int = 10) -> list[AcademicItem]:
        """按 query 检索,返回归一化条目。"""

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
