"""``data``: local-store reads and writes (get/add/set/del/attach/tree/members).

Writes go through the backend's ``_require("write")``, so a read-only backend raises
``NotSupportedError`` and the CLI reports it as ``[skipped]``.
"""
from __future__ import annotations

import json
import sys

from .. import fulltext as fulltext_mod
from .. import metadata
from ..citation import render_intext
from ..config import get_backend
from ..errors import BadInput
from ..models import AcademicItem
from ..provenance import stamp
from .keys import normalize_export_key, resolve_backend
from .render import present


def _resolve(cfg, args):
    """The backend and bare key: an explicit --backend wins, then the key's own source prefix."""
    return resolve_backend(cfg, args.backend, args.key, fallback="zotero")


def _writable(backend) -> None:
    """Fail fast and loudly before a write: the local store must be up (no silent no-op)."""
    ensure_running = getattr(backend, "ensure_running", None)
    if ensure_running:
        ensure_running()


def cmd_data_get(cfg, args) -> int:
    backend, key = _resolve(cfg, args)
    if getattr(backend, "access", "") != "local":
        # "arxiv/1806.09180" resolves on arXiv but 404s on the DOI backends; the local API
        # wants its own item key, so only the public backends get the normalization.
        key = normalize_export_key(key, backend.name)
    item = stamp([backend.get(key)], backend)[0]
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
    backend = get_backend(args.backend or "zotero", cfg)
    _writable(backend)
    item = None
    if args.pdf:
        item = metadata.extract_metadata(args.pdf)
        item.source = backend.name
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
            source=backend.name, title=args.title, creators=creators, year=args.year,
            doi=args.doi, url=args.url, abstract=args.abstract, item_type=args.type,
        )
        if args.csl:
            with open(args.csl, encoding="utf-8") as f:
                csl = json.load(f)
            item = AcademicItem.from_csl(csl, source=backend.name)

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
        if not sys.stdin.isatty():
            raise BadInput("no terminal for the y/N review; re-run with --yes to write the candidate as is")
        print("  Confirm writing this item to the library? [y/N]", end=" ", flush=True)
        try:
            answer = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = "n"
        if answer not in ("y", "yes"):
            print("[skipped] user cancelled")
            return 2

    key = backend.add(item)
    print(f"Added [{backend.name}] key={key}")
    return 0


def cmd_data_set(cfg, args) -> int:
    backend, key = _resolve(cfg, args)
    _writable(backend)
    fields = {k: v for k, v in {
        "title": args.title, "abstract": args.abstract, "year": args.year,
        "doi": args.doi, "url": args.url,
    }.items() if v is not None}
    it = backend.patch(key, fields)
    print(f"Updated [{backend.name}] {key}")
    print(present(it, "table", no_cite=True))
    return 0


def cmd_data_del(cfg, args) -> int:
    backend, key = _resolve(cfg, args)
    _writable(backend)
    backend.delete(key)
    print(f"Deleted [{backend.name}] {key}")
    return 0


def cmd_data_attach(cfg, args) -> int:
    backend, key = _resolve(cfg, args)
    _writable(backend)
    att_key = backend.attach(key, args.src)
    print(f"Attached fulltext [{backend.name}] {key} attachment-key={att_key or '-'} src={args.src}")
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
    backend = get_backend(args.backend or "zotero", cfg)
    nodes = backend.collection_tree()
    if not nodes:
        print("[no collections]")
        return 0
    print(render_tree(nodes))
    return 0


def cmd_data_members(cfg, args) -> int:
    """List items in a collection (recursing into all subcollections by default). Read-only."""
    backend = get_backend(args.backend or "zotero", cfg)
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
        names = [i.get("family", "") for i in it.creators if i.get("family")]
        authors = (", ".join(names[:3]) + (" et al." if len(names) > 3 else "")) or "Anon"
        print(f"  {it.key}  {authors}  {it.year or '-'}  {it.title} | {subs}  {cite}")
    return 0
