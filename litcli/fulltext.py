"""Fulltext resolution chain: local store → public OA sources → (optional) Sci-Hub → point to library.

Priorities (per user requirements):
  1. **Local store**: iterate configured backends with attachment capability (e.g. Zotero),
     locate an item by DOI and take its attachment — local store wins, no network.
  2. **Public OA sources**: Unpaywall (best_oa_location.pdf) → OpenAlex (best_oa_location.pdf_url),
     then the legal corpus endpoints: Europe PMC (open-access JATS XML) → Zenodo (open files),
     and finally Unpaywall's landing page (HTML, no bytes) so a legal page is still reported
     when no file exists.
  3. **Sci-Hub (opt-in, last)**: a copyright-evading library, so it is only consulted after the
     legitimate channels — including the landing page — and its hits are labelled with
     ``kind="scihub"``. Off unless the ``scihub`` backend is configured or ``--scihub`` is passed.
  4. **Point to**: if none of the above works, return has_fulltext=False and suggest
     institutional access / interlibrary loan / a proxy.

Decoupled from backends: ``resolve_fulltext`` accepts an injectable resolver list for offline
stubbing; the default resolvers are local/unpaywall/openalex (+ the corpus endpoints) + landing
page + scihub when injected.
"""
from __future__ import annotations

import html
import re

from lxml import etree as ET

from . import unpaywall
from .aggregate import norm_doi
from .errors import LitcliError
from .models import AcademicItem

# A resolver: takes a doi, returns a candidate dict or None.
# Candidate fields usable by the CLI:
#   has_fulltext, kind ('local'|'unpaywall'|'openalex'|'europepmc'|'zenodo'|'scihub'),
#   url (for the remote kinds), data (for local attachment bytes), title, status, key,
#   format ('xml' when the payload is a JATS document rather than a PDF)
Resolver = callable


def _norm_doi(d: str) -> str:
    """Normalize a DOI consistently with aggregate.norm_doi (strip doi.org prefix, case, trailing dots)."""
    return norm_doi(d)


def _from_local(doi: str, backends: dict | None = None) -> dict | None:
    """Local store first: for backends with attachment capability, locate an item by DOI and take its attachment."""
    if not backends:
        return None
    want = _norm_doi(doi)
    for name, b in backends.items():
        if not b.can("attachment"):
            continue
        try:
            for item in b.search(doi, limit=8):
                if _norm_doi(item.doi) != want and _norm_doi(item.url) != want:
                    continue
                try:
                    data = b.get_attachment(item.key)
                except (LitcliError, RuntimeError, OSError):
                    continue
                if data:
                    return {"kind": "local", "data": data, "title": item.title,
                            "key": item.key, "source": name, "access": "local"}
        except (LitcliError, RuntimeError, OSError):
            continue
    return None


def _from_unpaywall(doi: str, email: str, fetch=None) -> dict | None:
    """Unpaywall reverse lookup of a legitimate OA PDF (best_oa_location.url_for_pdf).

    ``fetch`` is an optional shared getter (used by the chain to cache the lookup so the
    landing-page step does not repeat the same request).
    """
    try:
        info = (fetch or (lambda d: unpaywall.resolve_oa(d, email)))(doi)
    except (LitcliError, RuntimeError, OSError):
        return None
    if not info:
        return None
    url = info.get("best_pdf") or ""
    if not url:
        return None
    return {"kind": "unpaywall", "url": url, "title": info.get("title", ""),
            "status": info.get("oa_status", ""), "access": "oa"}


def _from_unpaywall_landing(doi: str, email: str, fetch=None) -> dict | None:
    """Unpaywall's landing page: no bytes, but the last legal resort when no PDF source hits."""
    try:
        info = (fetch or (lambda d: unpaywall.resolve_oa(d, email)))(doi)
    except (LitcliError, RuntimeError, OSError):
        return None
    if not info:
        return None
    url = info.get("best_landing") or ""
    if not url:
        return None
    return {"kind": "unpaywall", "url": url, "title": info.get("title", ""),
            "status": info.get("oa_status", ""), "format": "html", "access": "oa"}


def _from_openalex(doi: str, backend) -> dict | None:
    """OpenAlex best_oa_location.pdf_url via an injected OA backend (domain stays backend-agnostic)."""
    if backend is None:
        return None
    try:
        item: AcademicItem = backend.get(doi)
    except (LitcliError, RuntimeError, OSError, KeyError):
        return None
    if item.attachments:
        return {"kind": "openalex", "url": item.attachments[0],
                "title": item.title, "status": "", "access": "oa"}
    return None


def _from_europepmc(doi: str, backend) -> dict | None:
    """Europe PMC open-access JATS XML (a legal fulltext that needs no PDF toolchain)."""
    if backend is None:
        return None
    try:
        item: AcademicItem = backend.get(doi)
    except (LitcliError, RuntimeError, OSError, KeyError):
        return None
    if not item.attachments:
        return None
    return {"kind": "europepmc", "url": item.attachments[0], "title": item.title,
            "status": "open access (JATS XML)", "format": "xml", "access": "oa"}


def _from_zenodo(doi: str, backend) -> dict | None:
    """A PDF file attached to an openly licensed Zenodo record."""
    if backend is None:
        return None
    try:
        item: AcademicItem = backend.get(doi)
    except (LitcliError, RuntimeError, OSError, KeyError):
        return None
    if not item.attachments:
        return None
    return {"kind": "zenodo", "url": item.attachments[0], "title": item.title,
            "status": "open access", "access": "oa"}


def _from_scihub(doi: str, backend) -> dict | None:
    """Sci-Hub fallback via an injected backend (last in the chain; opt-in)."""
    if backend is None:
        return None
    try:
        item: AcademicItem = backend.get(doi)
    except (LitcliError, RuntimeError, OSError, KeyError):
        return None
    if not item.attachments:
        return None
    return {"kind": "scihub", "url": item.attachments[0], "title": item.title,
            "status": "", "access": "public"}


def _default_resolvers(email: str, backends: dict | None = None,
                       openalex_backend=None, scihub_backend=None,
                       europepmc_backend=None, zenodo_backend=None) -> list[Resolver]:
    cache: dict[str, dict | None] = {}

    def _oa(doi: str):
        """One Unpaywall lookup per DOI, shared by the PDF step and the landing-page step."""
        if doi not in cache:
            try:
                cache[doi] = unpaywall.resolve_oa(doi, email)
            except (LitcliError, RuntimeError, OSError):
                cache[doi] = None
        return cache[doi]

    return [
        lambda d: _from_local(d, backends),
        lambda d: _from_unpaywall(d, email, _oa),
        lambda d: _from_openalex(d, openalex_backend),
        lambda d: _from_europepmc(d, europepmc_backend),
        lambda d: _from_zenodo(d, zenodo_backend),
        # The legal landing page outranks the shadow-library copy, which stays last.
        lambda d: _from_unpaywall_landing(d, email, _oa),
        lambda d: _from_scihub(d, scihub_backend),
    ]


# JATS/HTML block elements: keep their boundaries as line breaks so a text preview stays readable.
_XML_BLOCK_RE = re.compile(r"</(?:p|title|article-title|sec|abstract|ref|table-wrap|fig|list-item|tr)>")
_HTML_BLOCK_RE = re.compile(
    r"</(?:p|div|h[1-6]|li|tr|section|article|header|footer|title)>|<br\s*/?>", re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<(?:script|style)\b.*?</(?:script|style)>", re.S | re.I)


def _collapse_lines(raw: str) -> str:
    lines = [" ".join(line.split()) for line in raw.splitlines()]
    return "\n".join(line for line in lines if line)


def xml_to_text(data: bytes) -> str:
    """Flatten a JATS/XML document to plain text (previews without a PDF toolchain).

    Block-level closing tags become newlines; a single joined line would make ``--lines``
    return one 40 KB line instead of the first paragraphs.
    """
    try:
        root = ET.fromstring(data)
    except ET.XMLSyntaxError:
        return ""
    raw = _XML_BLOCK_RE.sub("\n", ET.tostring(root, encoding="unicode"))
    return _collapse_lines(html.unescape(_TAG_RE.sub(" ", raw)))


def html_to_text(data: bytes) -> str:
    """Strip an HTML landing page down to readable text (previews of the ``format="html"`` hit)."""
    raw = _SCRIPT_RE.sub(" ", data.decode("utf-8", "replace"))
    raw = _HTML_BLOCK_RE.sub("\n", raw)
    return _collapse_lines(html.unescape(_TAG_RE.sub(" ", raw)))


def resolve_fulltext(doi: str, email: str = unpaywall.DEFAULT_EMAIL,
                     backends: dict | None = None,
                     openalex_backend=None,
                     scihub_backend=None,
                     europepmc_backend=None,
                     zenodo_backend=None,
                     resolvers: list[Resolver] | None = None) -> dict:
    """Find fulltext by DOI, priority: local store → OA sources → Sci-Hub (when injected).

    Returns a dict: on a hit ``{"has_fulltext": True, "kind", ...}`` (data for local, url for OA
    and Sci-Hub); when nothing works, ``{"has_fulltext": False}`` (with per-step failure
    reasons, for the CLI to point the user elsewhere).
    """
    if resolvers is None:
        resolvers = _default_resolvers(email, backends, openalex_backend, scihub_backend,
                                       europepmc_backend, zenodo_backend)
    errors: list[str] = []
    for r in resolvers:
        try:
            cand = r(doi)
        except Exception as e:  # A single resolver failure must not bring down the whole chain
            errors.append(f"{getattr(r, '__name__', type(r).__name__)}: {e}")
            continue
        if cand and (cand.get("url") or cand.get("data")):
            cand["has_fulltext"] = True
            return cand
    return {"has_fulltext": False, "errors": errors}


# --- Adoption gate (prevents the local store from exploding) ---


def should_import(candidate: AcademicItem, existing: list[AcademicItem]) -> tuple[bool, str]:
    """Decide whether a candidate work is worth adopting into the local store (hard dedup gate).

    Returns (allowed, reason). Rules:
      - Candidate has DOI: normalized-equal to any existing item's DOI → reject (duplicate).
      - Candidate has no DOI: compare by normalized title → identical title → reject.
      - Incomplete metadata (no title / no author / no year) → default reject (do not auto-adopt
        incomplete entries), unless the caller explicitly passes --force (decided by the CLI).
    Reuses :mod:`aggregate`'s identity / norm_doi / norm_title for comparison, to prevent the
    store from blowing up.
    """
    from . import aggregate

    cand_doi = aggregate.norm_doi(candidate.doi)
    cand_title = aggregate.norm_title(candidate.title)
    for ex in existing:
        ex_doi = aggregate.norm_doi(ex.doi)
        if cand_doi and ex_doi and cand_doi == ex_doi:
            return False, f"A same-DOI item already exists ({ex.key or ex.title[:30]})"
        if not cand_doi:
            ex_title = aggregate.norm_title(ex.title)
            if ex_title and cand_title and ex_title == cand_title:
                return False, f"A same-title item already exists ({ex.key or ex.title[:30]})"
    if not candidate.title:
        return False, "Missing title, incomplete entry; fill it in or use --force"
    if not candidate.creators and not candidate.year:
        return False, "No author and no year, incomplete entry; fill it in or use --force"
    return True, ""
