"""The unified academic item model ``AcademicItem``, bidirectionally convertible to CSL JSON.

A domain-layer object — backend-agnostic. Whether a Zotero item, an arXiv Atom entry, or a
future filesystem backend, everything normalizes to an ``AcademicItem`` shared by aggregation,
export, and citation rendering.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict


@dataclass
class AcademicItem:
    """Backend-agnostic normalized model for an academic item.

    ``creators`` normalizes to ``[{"family": "Smith", "given": "John"}, ...]``.
    ``attachments`` are URI / file paths that locate a PDF (backend-specific, but str-typed alike).
    ``extra`` carries other backend-specific fields (e.g. Zotero's itemType, arXiv's primaryCategory).
    """

    source: str = ""                      # "zotero" / "arxiv" / ...
    key: str = ""                         # unique id within the backend: zotero item key / arxiv id
    title: str = ""
    creators: list[dict] = field(default_factory=list)
    year: str = ""
    abstract: str = ""
    doi: str = ""
    url: str = ""
    container_title: str = ""             # CSL container-title: journal/book name (holds volume/pages)
    volume: str = ""
    issue: str = ""
    page: str = ""                        # page range, e.g. "123-130"
    publisher: str = ""
    cited_by_count: int = 0               # citation count (OpenAlex cited_by_count / Crossref is-referenced-by-count)
    item_type: str = "document"           # CSL type: article / book / document / ...
    attachments: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    # --- CSL JSON conversion ---

    def to_csl(self) -> dict:
        """Convert to a CSL JSON dict (shared by citation/bibliography/export)."""
        csl: dict = {
            "type": self.item_type or "document",
            "title": self.title,
            "author": list(self.creators),
        }
        year = self.year.strip()
        if year:
            # CSL uses date-parts: [[year, month, day]]
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
        # stable citationkey fallback (no BBT): based on key or first author + year
        csl.setdefault("id", self.key or self._default_id())
        return csl

    def to_bibtex(self) -> str:
        """Export to BibTeX (fallback formatting without BBT): multi-line, with journal/volume/issue/pages/publisher."""
        entry = self._bibtex_type()
        fid = self.key or self._default_id().replace(":", "")
        out = [f"@{entry}{{{fid},", f"  title   = {{{self.title}}},"]
        if self.creators:
            out.append(f"  author  = {{{' and '.join(self._author_str(a) for a in self.creators)}}},")
        if self.year:
            out.append(f"  year    = {{{self.year}}},")
        # journal name goes in journal (article/paper-conference); book/chapter container in booktitle
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
        """Export to RIS (EndNote/Zotero common interchange format)."""
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
        """Build an AcademicItem from CSL JSON (often needed before writing to a backend)."""
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

    # --- internal helpers ---

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
        """Split a page string into (start, end) pairs for RIS SP/EP."""
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
