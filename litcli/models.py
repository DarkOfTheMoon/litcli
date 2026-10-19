"""The unified academic item model ``AcademicItem``, bidirectionally convertible to CSL JSON.

A domain-layer object — backend-agnostic. Whether a Zotero item, an arXiv Atom entry, or a
future filesystem backend, everything normalizes to an ``AcademicItem`` shared by aggregation,
export, and citation rendering.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict


def extract_year(text: str) -> str:
    r"""Pull a clean 4-digit publication year out of a date string.

    Accepts full dates (``2026-10-10``), date-times (``2026-09-23 14:49``), and CJK-style
    years (``2016年6月``). A year directly followed/preceded by CJK has no alphabetic word
    boundary (CJK is ``\w``), so ``\b...\b`` would miss it — use negative lookarounds.
    """
    m = re.search(r"(?<!\d)(19|20)\d{2}(?!\d)", text or "")
    return m.group(0) if m else ""


def normalize_author_name(raw: str) -> dict:
    """Turn an author name string into a CSL author dict (``family`` / ``given``).

    Backend-agnostic: Chinese names (CJK chars, possibly separated by a fullwidth comma or
    an ideographic comma, e.g. ``熊英, 关晖``) have no space-separated given name, so the
    whole CJK name goes in ``family`` and ``given`` stays empty — CSL renders a bare CJK
    ``family`` correctly. Latin names fall back to last-token-as-family (the convention
    shared by openlibrary / crossref).
    """
    s = (raw or "").strip()
    if not s:
        return {"family": "", "given": ""}
    if re.search(r"[一-鿿]", s):
        # CJK name: drop punctuation, keep the whole name in family; no given part.
        fam = re.sub(r"[，,、\s]+", "", s)
        return {"family": fam, "given": ""}
    # Latin name: "John Smith" -> family Smith, given John (last token is family).
    tokens = s.split()
    return {"family": tokens[-1], "given": " ".join(tokens[:-1])}


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
            "author": self._csl_authors(),
        }
        year = self.year.strip()
        if year:
            # CSL uses date-parts: [[year, month, day]]; "2020-03-12" splits into components,
            # but a non-numeric year ("2020 (in press)") must not crash the export.
            parts = [int(p) for p in year.split("-")[:3] if p.isdigit()]
            if not parts:
                m = re.search(r"(?:19|20)\d{2}", year)
                parts = [int(m.group(0))] if m else []
            if parts:
                csl["issued"] = {"date-parts": [parts]}
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
        out = [f"@{entry}{{{fid},", f"  title   = {{{self._one_line(self.title)}}},"]
        if self.creators:
            out.append(f"  author  = {{{' and '.join(self._author_str(a) for a in self.creators)}}},")
        if self.year:
            out.append(f"  year    = {{{self.year}}},")
        # journal name goes in journal (article/paper-conference); book/chapter container in booktitle
        if self.container_title:
            container_key = "booktitle" if self.item_type in ("chapter", "book") else "journal"
            out.append(f"  {container_key} = {{{self._one_line(self.container_title)}}},")
        if self.volume:
            out.append(f"  volume  = {{{self.volume}}},")
        if self.issue:
            out.append(f"  number  = {{{self.issue}}},")
        if self.page:
            out.append(f"  pages   = {{{self.page}}},")
        if self.publisher:
            out.append(f"  publisher = {{{self._one_line(self.publisher)}}},")
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
            lit = c.get("literal", "")
            fam = c.get("family", "")
            giv = c.get("given", "")
            if lit:
                lines.append(f"AU  - {lit}")
            elif giv:
                lines.append(f"AU  - {fam}, {giv}")
            else:
                lines.append(f"AU  - {fam}")
        if self.year:
            lines.append(f"PY  - {self.year}")
        if self.title:
            lines.append(f"TI  - {self._one_line(self.title)}")
        if self.container_title:
            lines.append(f"JF  - {self._one_line(self.container_title)}")
        if self.volume:
            lines.append(f"VL  - {self.volume}")
        if self.issue:
            lines.append(f"IS  - {self.issue}")
        for sp, ep in self._page_range():
            lines.append(f"SP  - {sp}")
            if ep:
                lines.append(f"EP  - {ep}")
        if self.publisher:
            lines.append(f"PB  - {self._one_line(self.publisher)}")
        if self.abstract:
            # A multi-line abstract must not leak a bare line into a line-oriented record.
            lines.append(f"AB  - {self._one_line(self.abstract)}")
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
        if a.get("literal"):
            return a.get("literal", "")
        given = a.get("given", "")
        family = a.get("family", "")
        return f"{family}, {given}".strip(", ")

    def _csl_authors(self) -> list[dict]:
        """Normalize creators to CSL author objects: keep only family/given/literal and drop malformed entries.

        A creator may arrive as a dict with a ``literal`` (non-person agent, e.g. an institution) or
        with unexpected keys; CSL only understands ``family``/``given``/``literal``. Emitting the raw
        dict would produce a malformed ``author`` array.
        """
        out = []
        for a in self.creators:
            if not isinstance(a, dict):
                continue
            if a.get("literal"):
                out.append({"literal": a["literal"]})
            else:
                norm = {}
                if a.get("family"):
                    norm["family"] = a["family"]
                if a.get("given"):
                    norm["given"] = a["given"]
                if norm:
                    out.append(norm)
        return out

    @staticmethod
    def _one_line(value: str) -> str:
        """Collapse whitespace (incl. embedded newlines/tabs) to single spaces for line-oriented formats."""
        return " ".join((value or "").split())

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
