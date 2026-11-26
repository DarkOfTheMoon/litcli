"""Europe PMC backend: biomedical/life-science literature over the EBI REST API.

Read-only, no key. Covers PubMed/PMC and the preprint servers Europe PMC indexes, and it is
the one legal source here that also *serves* machine-readable fulltext: for records flagged
open access, ``get`` exposes the JATS XML document (``.../fullTextXML``), which the fulltext
chain can hand back as text. The europepmc.org *web* pages sit behind a Cloudflare managed
challenge that plain urllib and even a real browser rarely clear, so links are built from the
identifiers rather than taken from the page. That costs only the typeset PDF and the text of
non-OA-but-in-EPMC records (``fullTextXML`` answers 500 for those).
"""
from __future__ import annotations

import re
import urllib.parse
from typing import ClassVar

from ..models import AcademicItem, extract_year, normalize_author_name
from .http import HttpBackend

_SEARCH = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
_FULLTEXT_XML = "https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML"
_EDGE = "https://www.ebi.ac.uk/europepmc/webservices/rest/{source}/{id}/{edge}"

_TAG_RE = re.compile(r"<[^>]+>")
_KEY_RE = re.compile(r"^(?:europepmc/)?((?:PMC)?\d+)$", re.I)
# The article-page URL form this backend prints as ``url`` (copy-paste round-trip).
_URL_RE = re.compile(r"europepmc\.org/article/[A-Za-z]+/((?:PMC)?\d+)", re.I)
_INITIALS_RE = re.compile(r"^[A-Z]{1,4}\.?$")
# Case-sensitive: "JR" in an authorString is usually the initials J. R., not the suffix "Jr".
_SUFFIX_RE = re.compile(r"^(?:Jr|Sr|II|III|IV)\.?$")


def _strip_tags(text: str) -> str:
    return " ".join(_TAG_RE.sub(" ", text or "").split())


def _creators(result: dict) -> list[dict]:
    """Prefer the structured author list; ``fullName`` is initials-first ("Dobler L")."""
    out = []
    for person in (result.get("authorList") or {}).get("author") or []:
        name = " ".join(p for p in (person.get("firstName"), person.get("lastName")) if p) \
            or person.get("fullName") or ""
        if name:
            out.append(normalize_author_name(name))
    if not out:  # lite results only carry "Family Initials, ..." — the initials come last
        for chunk in re.split(r"[;,]", result.get("authorString") or ""):
            tokens = chunk.strip().rstrip(".").split()
            if not tokens or tokens[0].lower() == "et":
                continue
            if len(tokens) > 1 and _SUFFIX_RE.match(tokens[-1]):
                # "Smith JA Jr": the initials precede the name suffix, which belongs to the family.
                suffix = tokens[-1].rstrip(".")
                family = (tokens[0] if len(tokens) == 2 else " ".join(tokens[:-2]))
                given = "" if len(tokens) == 2 else tokens[-2].rstrip(".")
                out.append({"family": family + " " + suffix, "given": given})
            elif len(tokens) == 1 and _SUFFIX_RE.match(tokens[0]) and out:
                # PubMed emits the suffix as its own comma chunk ("Tagare RL,  Jr, Meikahani R").
                out[-1]["family"] += " " + tokens[0].rstrip(".")
            elif _INITIALS_RE.match(tokens[-1]):
                out.append({"family": " ".join(tokens[:-1]), "given": tokens[-1].rstrip(".")})
            else:
                # Collective/group author ("The ABC Group") or a multi-word family without initials.
                out.append({"family": " ".join(tokens), "given": ""})
    return out


def _fulltext_xml(result: dict) -> str:
    """The JATS XML URL for an open-access article with a PMC id, else ""."""
    pmcid = result.get("pmcid") or ""
    if not pmcid or (result.get("isOpenAccess") or "").upper() != "Y":
        return ""
    return _FULLTEXT_XML.format(pmcid=pmcid)


def result_to_item(result: dict, source: str = "europepmc") -> AcademicItem:
    """Normalize one Europe PMC result record (pure: no I/O, offline-testable)."""
    ident = result.get("id") or result.get("pmcid") or ""
    src = result.get("source") or "MED"
    doi = result.get("doi") or ""
    xml = _fulltext_xml(result)
    return AcademicItem(
        source=source,
        key=doi or f"europepmc/{ident}",
        title=_strip_tags(result.get("title") or "").rstrip("."),
        creators=_creators(result),
        year=extract_year(str(result.get("pubYear") or result.get("firstPublicationDate") or "")),
        abstract=_strip_tags(result.get("abstractText") or ""),
        doi=doi,
        url=f"https://europepmc.org/article/{src}/{ident}" if ident else (f"https://doi.org/{doi}" if doi else ""),
        item_type="article",
        container_title=(result.get("journalInfo") or {}).get("journal", {}).get("title", "")
        or (result.get("journalTitle") or ""),
        volume=str((result.get("journalInfo") or {}).get("volume") or ""),
        issue=str((result.get("journalInfo") or {}).get("issue") or ""),
        page=result.get("pageInfo") or "",
        attachments=[xml] if xml else [],
        extra={"pmid": result.get("pmid", ""), "pmcid": result.get("pmcid", ""),
               "open_access": result.get("isOpenAccess", "")},
    )


def _edge_to_item(row: dict, source: str) -> AcademicItem | None:
    """One edge row (a citation record) as an item; ``None`` when it carries no identity."""
    doi = (row.get("doi") or "").strip()
    ident = str(row.get("id") or "")
    title = _strip_tags(row.get("title") or "").rstrip(".")
    if not doi and not ident and not title:
        return None
    src = str(row.get("source") or "MED")
    # The key must be re-askable by ``get``/``_ident``: the renderer adds the source name in
    # front already, so the key carries the bare identifier (PMC-prefixed where that is the id).
    key = ident if ident.startswith("PMC") else (f"PMC{ident}" if src == "PMC" else ident)
    return AcademicItem(
        source=source,
        key=doi or key,
        title=title,
        creators=_creators({"authorString": row.get("authorString") or ""}),
        year=extract_year(str(row.get("pubYear") or "")),
        doi=doi,
        url=f"https://europepmc.org/article/{src}/{ident}" if ident else (
            f"https://doi.org/{doi}" if doi else ""),
        item_type="article",
        container_title=row.get("journalAbbreviation") or "",
        volume=str(row.get("volume") or ""),
        issue=str(row.get("issue") or ""),
        page=row.get("pageInfo") or "",
        cited_by_count=row.get("citedByCount") or 0,
    )


class EuropePmcBackend(HttpBackend):
    """Read-only biomedical search with an open-access JATS XML fulltext hook."""

    access = "oa"

    headers: ClassVar[dict] = {"Accept": "application/json"}

    def __init__(self, name: str = "europepmc", base: str = _SEARCH,
                 timeout: int = 30, delay: float = 0.4,
                 retries: int | None = None, retry_after_cap: float | None = None):
        super().__init__(name=name, timeout=timeout, rate=3, min_interval=delay,
                         retries=retries, retry_after_cap=retry_after_cap)
        self._base = base

    @property
    def capabilities(self) -> set[str]:
        # ``attachment`` stays off: the XML is a URL, not a local-store copy (same as scihub).
        return {"read", "refs", "citing"}

    def _query(self, query: str, page_size: int = 10, result_type: str = "lite") -> list[dict]:
        # pageSize > 1000 answers HTTP 200 with only {"errCode": 404} (no resultList), i.e. a
        # silent empty result, so clamp to the documented range.
        params = urllib.parse.urlencode({"query": query, "format": "json",
                                         "pageSize": min(max(1, page_size), 1000),
                                         "resultType": result_type})
        data = self.fetch_json(f"{self._base}?{params}")
        return ((data or {}).get("resultList") or {}).get("result") or []

    def search(self, query: str, limit: int = 10, filters: dict | None = None) -> list[AcademicItem]:
        filters = self._filters(filters)
        parts = [f'"{query}"' if query.strip() else ""]
        for field, key in (("TITLE", "title"), ("AUTH", "author"), ("JOURNAL", "container")):
            if filters.get(key):
                parts.append(f'{field}:"{filters[key]}"')
        if filters.get("year"):
            parts.append(f"PUB_YEAR:{filters['year']}")
        q = " AND ".join(p for p in parts if p) or "*"
        return [result_to_item(r, self.name) for r in self._query(q, limit)][:max(1, limit)]

    def _ident(self, key: str) -> tuple[str, str] | None:
        """``(source, id)`` for the edge endpoints: a PMID/PMCID literally, or one lookup."""
        k = (key or "").strip()
        if k.startswith("https://doi.org/"):
            k = k[len("https://doi.org/"):]
        link = _URL_RE.search(k)
        if link:
            k = link.group(1)
        m = _KEY_RE.match(k)
        if m and not k.lower().startswith("10."):
            ident = m.group(1).upper()
            return ("PMC", ident) if ident.startswith("PMC") else ("MED", ident)
        if k.lower().startswith("10."):
            found = self._query(f'DOI:"{k}"', page_size=1, result_type="core")
            if not found:
                return None
            row = found[0]
            source = str(row.get("source") or "MED")
            ident = str(row.get("id") or row.get("pmid") or "")
            return (source, ident) if ident else None
        return None

    def _edge(self, key: str, edge: str, box: str, slot: str, limit: int) -> list[AcademicItem]:
        resolved = self._ident(key)
        if resolved is None:
            return []
        source, ident = resolved
        params = urllib.parse.urlencode({"format": "json",
                                         "pageSize": min(max(1, int(limit)), 100)})
        data = self.fetch_json(_EDGE.format(source=source, id=ident, edge=edge) + f"?{params}")
        # The two directions are boxed differently by EBI: citations under ``citationList``,
        # references under ``referenceList``. Reading the wrong one answers empty forever.
        rows = ((data or {}).get(box) or {}).get(slot) or []
        out = []
        for row in rows:
            item = _edge_to_item(row, self.name)
            if item is not None:
                out.append(item)
            if len(out) >= limit:
                break
        return out

    def references(self, key: str, limit: int = 5, sort_cited: bool = True) -> list[AcademicItem]:
        return self._edge(key, "references", "referenceList", "reference", limit)

    def citing(self, key: str, limit: int = 5, sort_cited: bool = True) -> list[AcademicItem]:
        return self._edge(key, "citations", "citationList", "citation", limit)

    def get(self, key: str) -> AcademicItem:
        k = (key or "").strip()
        if k.startswith("https://doi.org/"):
            k = k[len("https://doi.org/"):]
        link = _URL_RE.search(k)
        if link:  # the printable article URL resolves like a bare identifier
            k = link.group(1).upper()
        if k.lower().startswith("10."):
            query = f'DOI:"{k}"'
        else:
            m = _KEY_RE.match(k)
            if not m:
                raise KeyError(f"[{self.name}] unsupported key {key!r} (want a DOI or PMC/PMID)")
            ident = m.group(1).upper()
            query = f"PMCID:{ident}" if ident.startswith("PMC") else f"EXT_ID:{ident}"
        results = self._query(query, page_size=1, result_type="core")
        if not results:
            raise KeyError(f"[{self.name}] not found: {key}")
        return result_to_item(results[0], self.name)
