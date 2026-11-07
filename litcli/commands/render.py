"""Item presentation shared by the read commands (table row, JSON, CSL)."""
from __future__ import annotations

import json

from ..citation import render_intext
from ..models import AcademicItem
from ..provenance import tag


def present(item: AcademicItem, fmt: str = "table", no_cite: bool = False) -> str:
    if fmt == "json":
        return json.dumps(item.to_dict(), ensure_ascii=False)
    if fmt == "csl":
        return json.dumps(item.to_csl(), ensure_ascii=False)
    authors = ", ".join(i.get("family", "") for i in item.creators) or "Anon"
    first = f"{item.source}/{item.key}"
    source = f"{first:<22}{tag(item.access):<9}"
    line = f"{source} {authors:<24} {item.year:<5} {item.title[:60]}"
    if item.cited_by_count:
        line += f"   [cited {item.cited_by_count}]"
    if not no_cite:
        cite = render_intext([item])
        line = f"{line}   {cite[0]}"
    return line


def print_items(items: list[AcademicItem], fmt: str, no_cite: bool, empty_msg: str) -> int:
    """Render a list of items (json/csl single-line blocks, or table rows) + ``empty_msg`` fallback."""
    if fmt in ("json", "csl"):
        if not items:
            print("[]")  # machine-readable empty list, not a silent blank stdout
        for it in items:
            print(present(it, fmt, no_cite))
        return 0
    for it in items:
        print(present(it, "table", no_cite))
    if not items:
        print(empty_msg)
    return 0
