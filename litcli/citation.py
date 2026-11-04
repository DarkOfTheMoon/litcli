"""Citation / bibliography rendering (CSL JSON based, pure Python, no Better BibTeX).

The canonical path: export CSL JSON with ``litcli export --format csljson`` and feed
it to ``pandoc --citeproc`` / citeproc to produce canonical citations and bibliography.
This module additionally provides a dependency-light in-text (author-date) renderer
suitable for quick CLI previews.
"""
from __future__ import annotations

from collections.abc import Iterable

from .models import AcademicItem


def item_to_csl(item: AcademicItem) -> dict:
    return item.to_csl()


def render_intext(items: Iterable[AcademicItem]) -> list[str]:
    """Minimal author-date in-text citation, e.g. ``Smith, Jones & Lee 2002``."""
    out = []
    for it in items:
        names = [a.get("family", "") for a in it.creators if isinstance(a, dict)]
        if not names:
            label = "Anon"
        elif len(names) == 1:
            label = names[0]
        elif len(names) == 2:
            label = f"{names[0]} & {names[1]}"
        elif len(names) == 3:
            label = f"{names[0]}, {names[1]} & {names[2]}"
        else:
            label = f"{names[0]} et al."
        out.append(f"({label} {it.year})".strip())
    return out


def render_bibliography(items: Iterable[AcademicItem]) -> list[str]:
    """Minimal bibliography entry list, e.g. ``Smith, J. (2002). Title.``"""
    out = []
    for it in items:
        creators = "; ".join(
            f"{a.get('family', '')}, {a.get('given', '')}".strip(", ")
            for a in it.creators if isinstance(a, dict)
        )
        year = f"({it.year})." if it.year else ""
        head = f"{creators} {year}".strip() or "Anon"
        entry = f"{head} {it.title}."
        if it.container_title:
            entry += f" {it.container_title}."
        tail = []
        if it.volume:
            tail.append(f"{it.volume}")
        if it.issue:
            tail.append(f"({it.issue})")
        if it.page:
            tail.append(f"{it.page}")
        if tail:
            entry += " " + ", ".join(tail) + "."
        if it.publisher:
            entry += f" {it.publisher}."
        if it.doi:
            entry += f" doi:{it.doi}"
        if it.url:
            entry += f" {it.url}"
        out.append(entry)
    return out
