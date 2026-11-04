"""``data``: local-store reads and writes (get/add/set/del/attach/tree/members).

Writes go through the backend's ``_require("write")``, so a read-only backend raises
``NotSupportedError`` and the CLI reports it as ``[skipped]``.
"""
from __future__ import annotations

import json

from .. import fulltext as fulltext_mod
from .. import metadata
from ..citation import render_intext
from ..config import get_backend
from ..models import AcademicItem
from .render import present


def cmd_data_get(cfg, args) -> int:
    backend = get_backend(args.backend, cfg)
    item = backend.get(args.key)
    print(present(item, args.format, no_cite=True))
    if args.format == "table" and item.abstract:
        print(f"\n  {item.abstract[:260]}")
    if args.format == "table" and item.attachments:
        print(f"  attachments: {', '.join(item.attachments)}")
    return 0


def cmd_data_add(cfg, args) -> int:
    """Ingest metadata (``data add --title..`` / ``--csl``) or metadata + fulltext (``data add --pdf``).

    The only write entry point for metadata: with ``--pdf`` it extracts candidates heuristically,
    shows them for review, and writes only after an interactive confirmation. ``--force`` skips the
    dedup / completeness gate; ``--yes`` skips the interactive prompt (both together let you write
    without any prompt).
    Written entries pass the ``should_import`` dedup / completeness gate to prevent the local
    store from exploding.
    """
    backend = get_backend(args.backend, cfg)
    item = None
    if args.pdf:
        item = metadata.extract_metadata(args.pdf)
        item.source = args.backend
        print("--- Heuristic extraction from PDF first page (for review) ---")
        print(f"  title: {item.title}")
        print(f"  authors: {', '.join(a.get('family','') for a in item.creators) or '-'}")
        print(f"  year: {item.year or '-'}")
        if item.abstract:
            print(f"  abstract: {item.abstract[:120]}")
        if not (item.title or item.creators or item.year):
            print("[warning] No fields were extracted; use --csl or fill them in by hand instead of writing blindly")
    else:
        creators = []
        for c in args.creator:
            parts = [x.strip() for x in c.split(",")]
            fam = parts[0]
            giv = parts[1] if len(parts) > 1 else ""
            creators.append({"family": fam, "given": giv})
        item = AcademicItem(
            source=args.backend, title=args.title, creators=creators, year=args.year,
            doi=args.doi, url=args.url, abstract=args.abstract, item_type=args.type,
        )
        if args.csl:
            with open(args.csl, encoding="utf-8") as f:
                csl = json.load(f)
            item = AcademicItem.from_csl(csl, source=args.backend)

    # Dedup / completeness gate (skipped only with --force)
    if not args.force:
        try:
            existing = backend.search(item.doi or item.title, limit=20)
        except Exception:
            existing = []
        ok, reason = fulltext_mod.should_import(item, existing)
        if not ok:
            print(f"[skipped] {reason}")
            print("  To force-add, use --force")
            return 2

    # Heuristic metadata (--pdf) is noisy: confirm before mutating the library, unless --yes/--force.
    # In a non-interactive context (no TTY, piped) input() raises EOFError -> cancel (safe default).
    if args.pdf and not (args.force or args.yes):
        print("  Confirm writing this item to the library? [y/N]", end=" ", flush=True)
        try:
            answer = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = "n"
        if answer not in ("y", "yes"):
            print("[skipped] user cancelled")
            return 2

    key = backend.add(item)
    print(f"Added [{args.backend}] key={key}")
    return 0


def cmd_data_set(cfg, args) -> int:
    backend = get_backend(args.backend, cfg)
    fields = {k: v for k, v in {
        "title": args.title, "abstract": args.abstract, "year": args.year,
        "doi": args.doi, "url": args.url,
    }.items() if v is not None}
    it = backend.patch(args.key, fields)
    print(f"Updated [{args.backend}] {args.key}")
    print(present(it, "table", no_cite=True))
    return 0


def cmd_data_del(cfg, args) -> int:
    backend = get_backend(args.backend, cfg)
    backend.delete(args.key)
    print(f"Deleted [{args.backend}] {args.key}")
    return 0


def cmd_data_attach(cfg, args) -> int:
    backend = get_backend(args.backend, cfg)
    att_key = backend.attach(args.key, args.src)
    print(f"Attached fulltext [{args.backend}] {args.key} attachment-key={att_key or '-'} src={args.src}")
    return 0


def _collection_path_map(backend) -> dict:
    """Build a {key: "parent / child / grandchild"} path map from backend.collection_tree()."""
    nodes = backend.collection_tree()
    name = {n["key"]: n["name"] for n in nodes}
    parent = {n["key"]: n["parent"] for n in nodes}
    out = {}
    for k in name:
        parts, cur, seen = [], k, set()
        while cur and cur in name and cur not in seen:
            seen.add(cur)
            parts.insert(0, name[cur])
            cur = parent.get(cur)
        out[k] = " / ".join(parts)
    return out


def render_tree(nodes: list[dict]) -> str:
    """Render a collection node list as hierarchically indented tree text."""
    parent = {n["key"]: n["parent"] for n in nodes}
    name = {n["key"]: n["name"] for n in nodes}
    roots = [n["key"] for n in nodes if not parent.get(n["key"])]

    def depth(k: str) -> int:
        d, c = 0, k
        while c and c in parent:
            c = parent.get(c)
            d += 1
        return d

    lines = []
    seen = set()

    def walk(key: str, prefix: str, is_last: bool):
        if key in seen or not name.get(key):
            return
        seen.add(key)
        if depth(key) > 0:
            connector = "└─ " if is_last else "├─ "
            lines.append(f"  {prefix}{connector}{name[key]}  ({key})")
        else:
            lines.append(f"  {name[key]}  ({key})")
        child_keys = sorted([n["key"] for n in nodes if parent.get(n["key"]) == key],
                            key=lambda k: name[k].lower())
        child_prefix = prefix + ("   " if is_last else "│  ")
        for i, ck in enumerate(child_keys):
            walk(ck, child_prefix, i == len(child_keys) - 1)

    roots.sort(key=lambda k: name[k].lower())
    for i, rk in enumerate(roots):
        walk(rk, "", i == len(roots) - 1)
    return "\n".join(lines)


def cmd_data_tree(cfg, args) -> int:
    """List the collection tree (hierarchical indented display). Read-only."""
    backend = get_backend(args.backend, cfg)
    nodes = backend.collection_tree()
    if not nodes:
        print("[no collections]")
        return 0
    print(render_tree(nodes))
    return 0


def cmd_data_members(cfg, args) -> int:
    """List items in a collection (recursing into all subcollections by default). Read-only."""
    backend = get_backend(args.backend, cfg)
    recursive = not getattr(args, "no_recursive", False)
    items = backend.collection_items(args.coll_key, recursive=recursive)

    if args.format == "json":
        for it in (items[: args.limit] if args.limit else items):
            print(present(it, "json"))
        return 0

    path_map = _collection_path_map(backend)
    sel = path_map.get(args.coll_key, args.coll_key)
    shown = items[: args.limit] if args.limit else items
    note = f"(of {len(items)} items, showing first {len(shown)})" if args.limit and len(items) > len(shown) else ""
    print(f"[{sel}] matched {len(items)} items {note}")
    for it in shown:
        colls = it.extra.get("collections", []) or []
        subs = ", ".join(path_map.get(c, c) for c in colls
                         if path_map.get(c, c) and path_map.get(c, c) != sel) or "-"
        try:
            cite = render_intext([it])[0]
        except Exception:
            cite = ""
        authors = ", ".join(i.get("family", "") for i in it.creators) or "Anon"
        print(f"  {it.key:<8} {authors[:22]:<22} {it.year:<5} {it.title[:50]:<50} | {subs[:46]}  {cite}")
    return 0
