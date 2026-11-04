"""``export``: citations as CSL JSON, BibTeX, RIS or author-date text."""
from __future__ import annotations

import json

from ..citation import render_bibliography, render_intext
from ..config import get_backend
from .keys import normalize_export_key


def cmd_export(cfg, args) -> int:
    backend = get_backend(args.backend, cfg)
    items = [backend.get(normalize_export_key(k, args.backend)) for k in args.key]
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
            print(render_intext([it])[0])
            print(render_bibliography([it])[0])
    return 0
