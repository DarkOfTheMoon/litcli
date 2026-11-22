"""``export``: citations as CSL JSON, BibTeX, RIS or author-date text."""
from __future__ import annotations

import json

from ..citation import render_bibliography, render_intext
from .keys import normalize_export_key, resolve_backend


def cmd_export(cfg, args) -> int:
    resolved = [resolve_backend(cfg, args.backend, k, fallback="zotero") for k in args.key]
    items = [b.get(normalize_export_key(k, b.name)) for b, k in resolved]
    if args.format == "csljson":
        if len(items) == 1:
            print(json.dumps(items[0].to_csl(), ensure_ascii=False, indent=2))
        else:
            print(json.dumps([i.to_csl() for i in items], ensure_ascii=False, indent=2))
    elif args.format == "bibtex":
        for it in items:
            print(it.to_bibtex())
    elif args.format == "ris":
        for it in items:
            print(it.to_ris())
    else:
        for it in items:
            if not args.no_cite:
                print(render_intext([it])[0])
            print(render_bibliography([it])[0])
    return 0
