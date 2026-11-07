"""What kind of copy a backend serves, so every hit says where it came from.

A result is only useful if the caller knows what they are holding: an openly licensed copy, a
copy posted on a public site without such a licence, or a file from their own library. Backends
declare this once (``Backend.access``) and every read path stamps it onto the records, so the
label travels with the item instead of being guessed from the backend's name.

Values are deliberately about the copy, not about a site:

``oa``      openly licensed (arXiv, DOAB, Europe PMC, Zenodo, DOAJ, OpenAlex)
``public``  a copy published on a public site without an open licence (Sci-Hub, Z-Library)
``local``   the user's own library
``""``      records or metadata only — no copy
"""
from __future__ import annotations

OA = "oa"
PUBLIC = "public"
LOCAL = "local"

LABELS = {OA: "open access", PUBLIC: "public copy", LOCAL: "your library"}
TAGS = {OA: "oa", PUBLIC: "public", LOCAL: "local"}


def label(access: str) -> str:
    """Long form for a notice line ("" for an unknown value)."""
    return LABELS.get(access, "")


def tag(access: str) -> str:
    """Compact table marker ("" when there is nothing to say)."""
    return f"[{TAGS[access]}]" if access in TAGS else ""


def stamp(items, backend) -> list:
    """Mark freshly returned items with the backend's access; returns the list unchanged."""
    access = getattr(backend, "access", "")
    if access:
        for item in items:
            item.access = access
    return items
