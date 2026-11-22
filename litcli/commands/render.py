"""Item presentation shared by the read commands (table row, JSON, CSL)."""
from __future__ import annotations

import json
import sys

from ..citation import render_intext
from ..models import AcademicItem
from ..provenance import tag


def _authors(item: AcademicItem) -> str:
    """Family names only, capped: a 40-author paper must not push the title off the screen."""
    names = [i.get("family", "") for i in item.creators if i.get("family")]
    if not names:
        return "Anon"
    return ", ".join(names[:3]) + (" et al." if len(names) > 3 else "")


#: Characters of abstract shown by ``--snippet`` / the search table.
SNIPPET = 160


def present(item: AcademicItem, fmt: str = "table", no_cite: bool = False,
            snippet: bool = False) -> str:
    """One record. The table keeps the title whole and adds the container and DOI: a truncated
    title cannot be judged, and the DOI is what ``fetch`` needs next. With ``snippet`` a short
    abstract line follows on an indented row — enough to tell relevant from not."""
    if fmt == "json":
        return json.dumps(item.to_dict(), ensure_ascii=False)
    if fmt == "csl":
        return json.dumps(item.to_csl(), ensure_ascii=False)
    label = tag(item.access)
    line = f"{item.source}/{item.key}  {label + ' ' if label else ''}{_authors(item)}  {item.year or '-'}  {item.title}"
    if item.container_title:
        line += f"  — {item.container_title}"
    if item.doi:
        line += f"  doi:{item.doi}"
    if item.cited_by_count:
        claims = item.extra.get("cited_by_source") or {}
        src = max(claims, key=claims.get) if claims else ""
        line += f"  [cited {item.cited_by_count}{f' via {src}' if src else ''}]"
        if len(set(claims.values())) > 1:
            line += " (sources disagree)"
    if not no_cite:
        cite = render_intext([item])
        line = f"{line}  {cite[0]}"
    if snippet and item.abstract:
        text = " ".join(item.abstract.split())
        line += f"\n    {text[:SNIPPET]}{'…' if len(text) > SNIPPET else ''}"
    return line


def _note_public(items: list[AcademicItem]) -> None:
    """One stderr line when any hit is a non-OA mirror copy (who owes the licence check)."""
    if any(it.access == "public" for it in items):
        print("[note] [public] = copy from a non-OA mirror (Sci-Hub / Z-Library), "
              "not an open-access licence — check the licence before reuse", file=sys.stderr)


def print_items(items: list[AcademicItem], fmt: str, no_cite: bool, empty_msg: str,
                snippet: bool = False) -> int:
    """Render a list of items (json/csl single-line blocks, or table rows) + ``empty_msg`` fallback."""
    if fmt in ("json", "csl"):
        # The legend is on stderr in every format: JSON has only the ``access`` value, and the
        # licence duty is the part a value cannot carry.
        _note_public(items)
        if not items:
            print("[]")  # machine-readable empty list, not a silent blank stdout
        for it in items:
            print(present(it, fmt, no_cite))
        return 0
    _note_public(items)
    for it in items:
        print(present(it, "table", no_cite, snippet))
    if not items:
        print(empty_msg)
    return 0
