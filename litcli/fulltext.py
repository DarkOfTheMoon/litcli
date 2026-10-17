"""Legitimate fulltext resolution chain: local store → public OA sources → point to library/proxy.

Three priorities (per user requirements):
  1. **Local store**: iterate configured backends with attachment capability (e.g. Zotero),
     locate an item by DOI and take its attachment — local store wins, no network.
  2. **Public OA sources**: Unpaywall (best_oa_location.pdf) → OpenAlex (best_oa_location.pdf_url),
     legally open-access versions provided by publishers.
  3. **Point to**: if none of the above works, return has_fulltext=False and suggest
     institutional access / interlibrary loan / a proxy — never bypass a paywall.

This is the compliant alternative to Sci-Hub-style copyright-evading sources. Decoupled from
backends: ``resolve_fulltext`` accepts an injectable resolver list for offline stubbing; the
default resolvers are local/unpaywall/openalex.
"""
from __future__ import annotations

import urllib.error

from . import unpaywall
from .aggregate import norm_doi
from .models import AcademicItem

# A resolver: takes a doi, returns a candidate dict or None.
# Candidate fields usable by the CLI:
#   has_fulltext, kind ('local'|'unpaywall'|'openalex'), url (for OA),
#   data (for local attachment bytes), title, status, key
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
                except (RuntimeError, NotImplementedError, OSError):
                    continue
                if data:
                    return {"kind": "local", "data": data, "title": item.title,
                            "key": item.key, "source": name}
        except (RuntimeError, urllib.error.URLError, OSError, NotImplementedError):
            continue
    return None


def _from_unpaywall(doi: str, email: str) -> dict | None:
    """Unpaywall reverse lookup of a legitimate OA PDF (best_oa_location.url_for_pdf)."""
    try:
        info = unpaywall.resolve_oa(doi, email)
    except (RuntimeError, urllib.error.URLError, OSError):
        return None
    url = info.get("best_pdf") or info.get("best_landing") or ""
    if not url:
        return None
    return {"kind": "unpaywall", "url": url, "title": info.get("title", ""),
            "status": info.get("oa_status", "")}


def _from_openalex(doi: str, backend) -> dict | None:
    """OpenAlex best_oa_location.pdf_url via an injected OA backend (domain stays backend-agnostic)."""
    if backend is None:
        return None
    try:
        item: AcademicItem = backend.get(doi)
    except (RuntimeError, urllib.error.URLError, OSError, KeyError):
        return None
    if item.attachments:
        return {"kind": "openalex", "url": item.attachments[0],
                "title": item.title, "status": ""}
    return None


def _default_resolvers(email: str, backends: dict | None = None,
                       openalex_backend=None) -> list[Resolver]:
    return [
        lambda d: _from_local(d, backends),
        lambda d: _from_unpaywall(d, email),
        lambda d: _from_openalex(d, openalex_backend),
    ]


def resolve_fulltext(doi: str, email: str = unpaywall.DEFAULT_EMAIL,
                     backends: dict | None = None,
                     openalex_backend=None,
                     resolvers: list[Resolver] | None = None) -> dict:
    """Find legitimate fulltext by DOI, priority: local store → OA sources.

    Returns a dict: on a hit ``{"has_fulltext": True, "kind", ...}`` (data for local, url for OA);
    when no legitimate fulltext exists, ``{"has_fulltext": False}`` (with per-step failure
    reasons, for the CLI to point the user elsewhere).
    """
    if resolvers is None:
        resolvers = _default_resolvers(email, backends, openalex_backend)
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
