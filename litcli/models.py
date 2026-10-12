"""统一学术条目模型 AcademicItem,与 CSL JSON 双向转换。

领域层对象——后端无关。无论是 Zotero 的条目、arXiv 的 Atom 条目,
还是将来的文件系统后端,都归一化为 AcademicItem,供聚合、导出、引文共用。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict


@dataclass
class AcademicItem:
    """学术条目的后端无关归一化模型。

    creators 归一化为 ``[{"family": "Smith", "given": "John"}, ...]``。
    attachments 为可定位到 PDF 的 URI / 文件路径(后端相关,但类型统一为 str)。
    extra 兜底其它后端特有字段(如 zotero 的 itemType、arxiv 的 primaryCategory)。
    """

    source: str = ""                      # "zotero" / "arxiv" / ...
    key: str = ""                         # 后端内唯一 id: zotero item key / arxiv id
    title: str = ""
    creators: list[dict] = field(default_factory=list)
    year: str = ""
    abstract: str = ""
    doi: str = ""
    url: str = ""
    container_title: str = ""             # CSL container-title:期刊名/书名(卷页所在)
    volume: str = ""
    issue: str = ""
    page: str = ""                        # 页码区间,如 "123-130"
    publisher: str = ""
    cited_by_count: int = 0               # citation count (OpenAlex cited_by_count / Crossref is-referenced-by-count)
    item_type: str = "document"           # CSL type: article / book / document / ...
    attachments: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    # --- CSL JSON 转换 ---

    def to_csl(self) -> dict:
        """转为 CSL JSON 字典(citation/参考文献/导出共用)。"""
        csl: dict = {
            "type": self.item_type or "document",
            "title": self.title,
            "author": list(self.creators),
        }
        year = self.year.strip()
        if year:
            # CSL 用 date-parts: [[year, month, day]]
            parts = [int(p) for p in year.split("-")[:3] if p.isdigit()]
            csl["issued"] = {"date-parts": [parts or [int(year)]]}
        if self.abstract:
            csl["abstract"] = self.abstract
        if self.container_title:
            csl["container-title"] = self.container_title
        if self.volume:
            csl["volume"] = self.volume
        if self.issue:
            csl["issue"] = self.issue
        if self.page:
            csl["page"] = self.page
        if self.publisher:
            csl["publisher"] = self.publisher
        if self.doi:
            csl["DOI"] = self.doi
        if self.url:
            csl["URL"] = self.url
        # 稳定的 citationkey 兜底(无 BBT):基于 key 或首作者+年份
        csl.setdefault("id", self.key or self._default_id())
        return csl

    def to_bibtex(self) -> str:
        """导出为 BibTeX(无 BBT 时的兜底文案):多行、含期刊/卷/期/页/出版社。"""
        entry = self._bibtex_type()
        fid = self.key or self._default_id().replace(":", "")
        out = [f"@{entry}{{{fid},", f"  title   = {{{self.title}}},"]
        if self.creators:
            out.append(f"  author  = {{{' and '.join(self._author_str(a) for a in self.creators)}}},")
        if self.year:
            out.append(f"  year    = {{{self.year}}},")
        # 期刊名放 journal(article/paper-conference);书/章节容器名放 booktitle
        if self.container_title:
            container_key = "booktitle" if self.item_type in ("chapter", "book") else "journal"
            out.append(f"  {container_key} = {{{self.container_title}}},")
        if self.volume:
            out.append(f"  volume  = {{{self.volume}}},")
        if self.issue:
            out.append(f"  number  = {{{self.issue}}},")
        if self.page:
            out.append(f"  pages   = {{{self.page}}},")
        if self.publisher:
            out.append(f"  publisher = {{{self.publisher}}},")
        if self.doi:
            out.append(f"  doi     = {{{self.doi}}},")
        if self.url:
            out.append(f"  url     = {{{self.url}}},")
        out.append("}")
        return "\n".join(out) + "\n"

    def to_ris(self) -> str:
        """导出为 RIS(EndNote/Zotero 通用交换格式)。"""
        ty = {"article": "JOUR", "book": "BOOK", "chapter": "CHAP",
              "patent": "PAT", "thesis": "THES", "report": "RPRT"}.get(self.item_type, "GEN")
        lines = [f"TY  - {ty}"]
        for c in self.creators:
            fam = c.get("family", "")
            giv = c.get("given", "")
            lines.append(f"AU  - {fam}, {giv}" if giv else f"AU  - {fam}")
        if self.year:
            lines.append(f"PY  - {self.year}")
        if self.title:
            lines.append(f"TI  - {self.title}")
        if self.container_title:
            lines.append(f"JF  - {self.container_title}")
        if self.volume:
            lines.append(f"VL  - {self.volume}")
        if self.issue:
            lines.append(f"IS  - {self.issue}")
        for sp, ep in self._page_range():
            lines.append(f"SP  - {sp}")
            if ep:
                lines.append(f"EP  - {ep}")
        if self.publisher:
            lines.append(f"PB  - {self.publisher}")
        if self.abstract:
            lines.append(f"AB  - {self.abstract}")
        if self.doi:
            lines.append(f"DO  - {self.doi}")
        if self.url:
            lines.append(f"UR  - {self.url}")
        lines.append("ER  -")
        return "\n".join(lines) + "\n"

    @classmethod
    def from_csl(cls, csl: dict, source: str = "", key: str = "") -> "AcademicItem":
        """从 CSL JSON 构建 AcademicItem(写入后端前常需此路)。"""
        authors = csl.get("author") or []
        year = ""
        issued = csl.get("issued") or {}
        parts = issued.get("date-parts")
        if parts and parts[0]:
            year = str(parts[0][0])
        doil = csl.get("DOI") or ""
        if isinstance(doil, dict):
            doil = csl.get("DOI", {}).get("literal", "")
        return cls(
            source=source,
            key=key or csl.get("id", ""),
            title=csl.get("title", ""),
            creators=list(authors),
            year=year,
            abstract=csl.get("abstract", ""),
            doi=doil,
            url=csl.get("URL", ""),
            container_title=csl.get("container-title", ""),
            volume=csl.get("volume", ""),
            issue=csl.get("issue", ""),
            page=csl.get("page", ""),
            publisher=csl.get("publisher", ""),
            item_type=csl.get("type", "document"),
        )

    # --- 内部 helpers ---

    def _combine_authors(self) -> str:
        if not self.creators:
            return "Anon"
        names = [self._author_str(a) for a in self.creators[:3]]
        if len(self.creators) > 3:
            names = names[:2] + ["et al."]
        return ", ".join(names)

    @staticmethod
    def _author_str(a: dict) -> str:
        given = a.get("given", "")
        family = a.get("family", "")
        return f"{family}, {given}".strip(", ")

    def _page_range(self) -> list[tuple[str, str]]:
        """把页码字符串切成 (起始页, 结束页) 序列,供 RIS SP/EP 用。"""
        pages = (self.page or "").strip()
        if not pages:
            return []
        for sep in ("-", "–", "—"):
            if sep in pages:
                sp, _, ep = pages.partition(sep)
                return [(sp.strip(), ep.strip())]
        return [(pages, "")]

    def _default_id(self) -> str:
        if not self.creators:
            return f"anon_{self.year or 'noyear'}"
        first = self.creators[0].get("family", "anon") if isinstance(
            self.creators[0], dict) else self.creators[0]
        return f"{first}_{self.year or 'noyear'}"

    def _bibtex_type(self) -> str:
        return {"article": "article", "book": "book", "document": "misc",
                "journal-article": "article"}.get(self.item_type, "misc")

    def to_dict(self) -> dict:
        return asdict(self)
