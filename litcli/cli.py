"""litcli CLI orchestration layer: intent-based argparse subcommands, dispatched to backends.

Command surface (backends via --backend, default zotero):
  search        cross-backend candidate search (read-only)
  data get/add/set/del/attach   local store CRUD (write · requires decision)
  fetch fulltext    locate fulltext by DOI; --scihub adds the copyright-evading fallback
  fetch book        find a book by title/ISBN on Z-Library (shadow library) and resolve one
  export        export citations (read-only, no PDF)
  completions   print shell completion scripts (bash/zsh/fish; enable via eval)
  backends      diagnostics
  backends authorize [zotero]   actively authorize against local Zotero to get a write key
                                and write it back to config.toml (writes config)
Write commands first check backend.can("write"); read-only backends raise NotSupportedError
(caught in the CLI as [skipped]).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

from . import pdf as pdfutil
from . import __version__
from . import metadata
from . import unpaywall
from . import fulltext as fulltext_mod
from . import config as config_mod
from .config import get_all_backends, get_backend, load_config, write_backend_key
from .models import AcademicItem
from .aggregate import cited_key, merge_items
from .citation import render_intext, render_bibliography
from .completions import SHELL_CHOICES, build_spec, render as render_completion
from .backends.base import Backend, NotSupportedError
from .transport import set_proxy as set_backend_proxy


def _add_common_backend(p: argparse.ArgumentParser) -> None:
    p.add_argument("--backend", default="zotero", help="backend name (default zotero)")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="litcli", description="General academic CLI (pluggable multi-backend)")
    parser.add_argument("--version", action="version", version=f"litcli {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # --- Top-level: search / export / diagnostics ---
    p = sub.add_parser("search", help="cross-backend candidate search (read-only)")
    p.add_argument("query")
    p.add_argument("--backend", default="zotero")
    p.add_argument("--all", action="store_true", help="search across all backends with aggregation")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--format", choices=["table", "json", "csl"], default="table")
    p.add_argument("--type", default=None, help="filter by item type (article/book/chapter/patent/...), reduce noise")
    p.add_argument("--title", default=None, help="narrow by title term (field-level filter, AND)")
    p.add_argument("--author", default=None, help="narrow by author name, e.g. Bonaventura")
    p.add_argument("--year", default=None, help="narrow by publication year, e.g. 2018")
    p.add_argument("--container", default=None, help="narrow by journal / book title")
    p.add_argument("--sort", choices=["cited"], default=None,
                   help="sort descending by citation count (default is descending by year)")
    p.add_argument("--no-cite", action="store_true", help="do not show in-text citations in table mode")

    for name, help_ in (("refs", "references (bibliography): works cited by this item, look backward for foundations"),
                        ("citing", "works citing this item (forward tracking, by citation count desc)"),
                        ("related", "semantically related works (OpenAlex related_works, neighbors)")):
        pg = sub.add_parser(name, help=help_)
        pg.add_argument("key")
        pg.add_argument("--backend", default="openalex", help="backend (default openalex; only openalex implements it)")
        pg.add_argument("--limit", type=int, default=5)
        pg.add_argument("--format", choices=["table", "json", "csl"], default="table")
        pg.add_argument("--sort", choices=["cited"], default=None,
                        help="sort descending by citation count (refs/citing are already citation-desc; this flag applies to related)")
        pg.add_argument("--no-cite", action="store_true", help="do not show in-text citations in table mode")

    p = sub.add_parser("export", help="export citations (read-only, no PDF)")
    p.add_argument("key", nargs="+", help="one or more item keys")
    _add_common_backend(p)
    p.add_argument("--format", choices=["csljson", "bibtex", "ris", "text"], default="csljson")

    cp = sub.add_parser("completions", help="generate shell completion scripts (enable via eval; bash/zsh/fish)")
    cp.add_argument("shell", choices=SHELL_CHOICES, help="target shell")

    p = sub.add_parser("backends", help="backend diagnostics / authorization (get a write key via `backends authorize` before writing)")
    bsub = p.add_subparsers(dest="backends_cmd")
    bsub.add_parser("list", help="list configured backends with capabilities (read-only)")
    bsub.add_parser("status", help="show each backend's authorization / connectivity status (read-only)")
    bp = bsub.add_parser("authorize", help="actively authorize against local Zotero and write back to config.toml")
    bp.add_argument("backend", nargs="?", default="zotero",
                    help="backend name (default zotero; only zotero supports local authorization currently)")

    # --- data: local store CRUD (write · requires decision) ---
    d = sub.add_parser("data", help="local store operations (CRUD; read-only backends are reported)")
    dsub = d.add_subparsers(dest="data_cmd", required=True)

    dp = dsub.add_parser("get", help="single item, all fields")
    dp.add_argument("key")
    _add_common_backend(dp)
    dp.add_argument("--format", choices=["table", "json", "csl"], default="table")

    dp = dsub.add_parser("add", help="ingest metadata / metadata + fulltext")
    dp.add_argument("--csl", default=None, help="import from a csljson file (takes precedence over fields)")
    dp.add_argument("--pdf", default=None, help="extract metadata from a local PDF (heuristic, for review)")
    dp.add_argument("--title", default="")
    dp.add_argument("--creator", action="append", default=[], help="'family name, given name', repeatable")
    dp.add_argument("--type", default="document")
    dp.add_argument("--year", default="")
    dp.add_argument("--doi", default="")
    dp.add_argument("--url", default="")
    dp.add_argument("--abstract", default="")
    dp.add_argument("--force", action="store_true", help="skip dedup / completeness gate and write directly")
    dp.add_argument("--yes", action="store_true", help="skip the interactive confirmation prompt (for --pdf; --force already implies this)")
    _add_common_backend(dp)

    dp = dsub.add_parser("set", help="modify item fields")
    dp.add_argument("key")
    dp.add_argument("--title", default=None)
    dp.add_argument("--abstract", default=None)
    dp.add_argument("--year", default=None)
    dp.add_argument("--doi", default=None)
    dp.add_argument("--url", default=None)
    _add_common_backend(dp)

    dp = dsub.add_parser("del", help="delete an item")
    dp.add_argument("key")
    _add_common_backend(dp)

    dp = dsub.add_parser("attach", help="attach a fulltext file to an existing item")
    dp.add_argument("key")
    dp.add_argument("src", help="local path or URL")
    _add_common_backend(dp)

    dp = dsub.add_parser("tree", help="list the collection tree (hierarchical display; read-only)")
    _add_common_backend(dp)

    dp = dsub.add_parser("members", help="list items in a collection (filtered by collection; read-only)")
    dp.add_argument("coll_key", help="collection key (see it via `data tree`)")
    dp.add_argument("--no-recursive", action="store_true", default=False,
                    help="only that collection itself, without recursing into subcollections")
    _add_common_backend(dp)
    dp.add_argument("--format", choices=["table", "json"], default="table")
    dp.add_argument("--limit", type=int, default=200, help="max items to list (0=unlimited)")

    # --- fetch: get fulltext / metadata (read-only, no store) ---
    f = sub.add_parser("fetch", help="get fulltext / metadata (read-only, no store)")
    fsub = f.add_subparsers(dest="fetch_cmd", required=True)

    fp = fsub.add_parser("fulltext", help="locate fulltext by DOI (local store → Unpaywall → OpenAlex → Europe PMC/Zenodo; --scihub adds a fallback)")
    fp.add_argument("doi")
    fp.add_argument("--email", default=None)
    fp.add_argument("--download", action="store_true",
                    help="download the candidate fulltext file (PDF, or XML/HTML from the OA corpus)")
    fp.add_argument("--out", default=None, help="save path")
    fp.add_argument("--lines", type=int, default=6, help="preview line count after download")
    fp.add_argument("--scihub", action="store_true",
                    help="also try Sci-Hub after the legitimate OA sources (copyright-evading mirror)")

    bp = fsub.add_parser("book", help="search a book by title/ISBN on a book backend (default Z-Library) and resolve one hit")
    bp.add_argument("query", help="title, author or ISBN")
    bp.add_argument("--backend", default="zlibrary", help="book backend (default zlibrary)")
    bp.add_argument("--limit", type=int, default=5, help="candidate count to list")
    bp.add_argument("--pick", type=int, default=1, help="1-based candidate to resolve (default 1)")
    bp.add_argument("--download", action="store_true", help="download the chosen file")
    bp.add_argument("--out", default=None, help="save path")
    bp.add_argument("--lines", type=int, default=0, help="preview line count after download (0=off)")

    return parser


def _present(item: AcademicItem, fmt: str = "table", no_cite: bool = False) -> str:
    if fmt == "json":
        return json.dumps(item.to_dict(), ensure_ascii=False)
    if fmt == "csl":
        return json.dumps(item.to_csl(), ensure_ascii=False)
    authors = ", ".join(i.get("family", "") for i in item.creators) or "Anon"
    first = f"{item.source}/{item.key}"
    line = f"{first:<22} {authors:<24} {item.year:<5} {item.title[:60]}"
    if item.cited_by_count:
        line += f"   [cited {item.cited_by_count}]"
    if not no_cite:
        cite = render_intext([item])
        line = f"{line}   {cite[0]}"
    return line


def _print_items(items: list[AcademicItem], fmt: str, no_cite: bool, empty_msg: str) -> int:
    """Render a list of items (json/csl single-line blocks, or table rows) + ``empty_msg`` fallback."""
    if fmt in ("json", "csl"):
        if not items:
            print("[]")  # machine-readable empty list, not a silent blank stdout
        for it in items:
            print(_present(it, fmt, no_cite))
        return 0
    for it in items:
        print(_present(it, "table", no_cite))
    if not items:
        print(empty_msg)
    return 0


def _search_filters(args) -> dict:
    """Collect the CLI search ``--title/--author/--year/--container`` into a filters dict (non-empty only)."""
    return {k: v for k, v in {
        "title": getattr(args, "title", None),
        "author": getattr(args, "author", None),
        "year": getattr(args, "year", None),
        "container": getattr(args, "container", None),
    }.items() if v}


def cmd_search(cfg, args) -> int:
    filters = _search_filters(args)
    if args.all:
        backends = get_all_backends(cfg)
        items: list[AcademicItem] = []
        for name, b in backends.items():
            try:
                items.extend(b.search(args.query, limit=args.limit, filters=filters))
            except NotImplementedError:
                continue
            except (RuntimeError, OSError, ValueError, SyntaxError) as e:
                # A single backend failure (not connected / HTTP error / timeout / non-JSON body /
                # malformed XML) must not bring down the whole aggregation; skip and continue.
                print(f"[skipped] backend {name}: {e}", file=sys.stderr)
                continue
    else:
        backend = get_backend(args.backend, cfg)
        items = backend.search(args.query, limit=args.limit, filters=filters)

    # Aggregation: dedup / merge / sort and mark sources only when --all yields many items.
    if args.all and items and len(items) > 1:
        items = merge_items(items)

    # --type filter: drop noisy items (book titles / off-topic). Post-merge so --all dedup
    # is preserved; kept as a CLI post-filter for cross-backend semantic consistency.
    if getattr(args, "type", None):
        want = args.type.lower()
        items = [it for it in items if it.item_type.lower() == want]

    # --sort cited: descending by citation count (default is merge's year/title order).
    if getattr(args, "sort", None) == "cited":
        items = sorted(items, key=cited_key)

    return _print_items(items, args.format, args.no_cite, "[no results]")


def _normalize_related_key(key: str) -> str:
    """Normalize a ``related`` key: strip a source prefix (openalex/ arxiv/ crossref/ zotero/)
    and turn an arXiv id into its DOI, so the openalex backend's W-id/DOI lookup can resolve
    the key forms the CLI itself emits (``extra["openalex_id"]``, table keys, exported arXiv keys)."""
    k = key.strip()
    for prefix in ("openalex/", "arxiv/", "crossref/", "zotero/", "scihub/"):
        if k.startswith(prefix):
            k = k[len(prefix):]
            break
    aid = _arxiv_id_from_key(k)
    if aid:
        return f"10.48550/arxiv.{aid}"
    return k


_EDGE_METHODS = {"refs": "references", "citing": "citing", "related": "related"}


def _cmd_edge(cfg, args, method: str) -> int:
    """Shared edge-traversal command: dispatch ``method`` (references/citing/related) on one backend."""
    backend = get_backend(args.backend, cfg)
    # Only openalex implements the graph edges; the rest raise NotSupportedError (caught in main).
    # Ordering (by citation count) is owned by the backend, selected here via ``sort_cited``.
    items = getattr(backend, method)(_normalize_related_key(args.key), args.limit,
                                     sort_cited=(args.sort == "cited"))
    return _print_items(items, args.format, args.no_cite, "[no related]")


def cmd_get(cfg, args) -> int:
    backend = get_backend(args.backend, cfg)
    item = backend.get(args.key)
    print(_present(item, args.format, no_cite=True))
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
            import json as _json
            with open(args.csl, encoding="utf-8") as f:
                csl = _json.load(f)
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


def cmd_set(cfg, args) -> int:
    backend = get_backend(args.backend, cfg)
    fields = {k: v for k, v in {
        "title": args.title, "abstract": args.abstract, "year": args.year,
        "doi": args.doi, "url": args.url,
    }.items() if v is not None}
    it = backend.patch(args.key, fields)
    print(f"Updated [{args.backend}] {args.key}")
    print(_present(it, "table", no_cite=True))
    return 0


def _arxiv_id_from_key(key: str) -> str | None:
    """Recognize an arXiv id in an export key (e.g. ``arxiv/1806.09180`` or ``arXiv:1806.09180``)."""
    m = re.match(r"^\s*(?:arxiv[:/]|arXiv[:/]|arXiv)?\s*(\d{4}\.\d{4,5})(?:v\d+)?\s*$", key)
    if m:
        return m.group(1)
    return None


def _normalize_export_key(key: str, backend_name: str) -> str:
    """Normalize an arXiv key to a form the backend can resolve before export.

    - arxiv backend: give the bare arXiv id (its get resolves via ``id:<key>`` search);
    - other backends (openalex/crossref): convert to DOI ``10.48550/arxiv.<id>``,
      otherwise openalex.get would treat ``arxiv/1806.09180`` as an OpenAlex id and
      return 404.
    """
    aid = _arxiv_id_from_key(key)
    if not aid:
        return key
    if backend_name == "arxiv":
        return aid
    return f"10.48550/arxiv.{aid}"


def cmd_export(cfg, args) -> int:
    backend = get_backend(args.backend, cfg)
    items = [backend.get(_normalize_export_key(k, args.backend)) for k in args.key]
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


def _render_tree(nodes: list[dict]) -> str:
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
    print(_render_tree(nodes))
    return 0


def cmd_data_members(cfg, args) -> int:
    """List items in a collection (recursing into all subcollections by default). Read-only."""
    backend = get_backend(args.backend, cfg)
    recursive = not getattr(args, "no_recursive", False)
    items = backend.collection_items(args.coll_key, recursive=recursive)

    if args.format == "json":
        for it in (items[: args.limit] if args.limit else items):
            print(json.dumps(it.to_dict(), ensure_ascii=False))
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


def _resolve_oa_email(cfg, args) -> str:
    return (args.email
            or (cfg.get("unpaywall") or {}).get("email")
            or os.environ.get("UNPAYWALL_EMAIL")
            or unpaywall.DEFAULT_EMAIL)


def _optional_backend(name: str, cfg):
    """Instantiate a registered optional backend, or None when it is not available at all."""
    try:
        return get_backend(name, cfg)
    except KeyError:
        return None


def cmd_fetch_fulltext(cfg, args) -> int:
    """Locate fulltext by DOI: local store → Unpaywall → OpenAlex → Europe PMC/Zenodo; Sci-Hub only when asked.

    Sci-Hub is opt-in (`--scihub`, or by configuring the ``scihub`` backend) because it serves
    copyright-evading copies; its hits are labelled as such.
    """
    email = _resolve_oa_email(cfg, args)
    backends = get_all_backends(cfg)
    scihub_backend = backends.get("scihub")
    if getattr(args, "scihub", False) and scihub_backend is None:
        scihub_backend = get_backend("scihub", cfg)
    # The legal corpus sources are keyless: consult them even when not enabled in config.
    corpus = {name: backends.get(name) or _optional_backend(name, cfg)
              for name in ("europepmc", "zenodo")}
    info = fulltext_mod.resolve_fulltext(args.doi, email, backends=backends,
                                         openalex_backend=backends.get("openalex"),
                                         europepmc_backend=corpus["europepmc"],
                                         zenodo_backend=corpus["zenodo"],
                                         scihub_backend=scihub_backend)
    if not info.get("has_fulltext"):
        print(f"DOI {args.doi}  no such work in the local store or open OA sources")
        if not scihub_backend:
            print("  Add --scihub to also try Sci-Hub, or use institutional access / interlibrary loan / a proxy.")
        else:
            print("  Use institutional access / interlibrary loan / a proxy.")
        if info.get("errors"):
            print(f"  [resolution skipped: {len(info['errors'])} steps]")
        return 0

    title = info.get("title") or ""
    kind = info["kind"]
    print(f"DOI {args.doi}  fulltext available (source={kind})")
    if kind == "scihub":
        print("  note: Sci-Hub copy (copyright-evading), not an open-access licence")
    if info.get("format") == "xml":
        print("  note: open-access JATS XML (text without a PDF; not a rendered article)")
    if title:
        print(f"  title: {title[:70]}")

    if kind == "local":  # Local-store hit: attachment already in hand, just write it out
        key = info.get("key") or ""
        src = info.get("source") or "zotero"
        print(f"  local store [{src}] attachment matched (key={key})")
        raw = info["data"]
        out = args.out or f"{args.doi.replace('/', '_')}.pdf"
        with open(out, "wb") as f:
            f.write(raw)
        print(f"  Saved {len(raw)} bytes to {out}")
        if raw and args.lines:
            text = pdfutil.extract_text(raw, max_chars=None)
            print(f"  --- {args.lines}-line preview ---\n{pdfutil.first_lines(text, args.lines)}")
        return 0

    # OA sources: give the direct link by default, download only with --download
    if info.get("status"):
        print(f"  oa_status: {info['status']}")
    fmt = info.get("format") or "pdf"
    print(f"  {'PDF' if fmt == 'pdf' else fmt.upper()}: {info['url']}")
    if fmt == "html":
        print("  note: landing page, not a file — open it in a browser")
    if args.download:
        raw = pdfutil.download(info["url"])
        out = args.out or f"{args.doi.replace('/', '_')}.{fmt}"
        with open(out, "wb") as f:
            f.write(raw)
        print(f"  Downloaded {len(raw)} bytes to {out}")
        if raw and args.lines:
            if fmt == "html":
                text = fulltext_mod.html_to_text(raw)
            elif fmt == "xml":
                text = fulltext_mod.xml_to_text(raw)
            else:
                text = pdfutil.extract_text(raw, max_chars=None)
            print(f"  --- {args.lines}-line preview ---\n{pdfutil.first_lines(text, args.lines)}")
    return 0


def cmd_fetch_book(cfg, args) -> int:
    """Find a book by title/ISBN on a book backend, resolve one candidate, optionally download.

    Z-Library copies are shadow-library copies (not open-access licensed), so the hit is
    labelled as such. ``--pick`` selects among the listed candidates; the chosen record is
    printed in full before any bytes are fetched.
    """
    backend = get_backend(args.backend, cfg)
    candidates = backend.search(args.query, limit=max(1, args.limit))
    if not candidates:
        print(f"[no books] {args.query!r}")
        return 0

    print(f"{len(candidates)} candidate(s) from {backend.name}:")
    for i, item in enumerate(candidates, 1):
        authors = ", ".join(c.get("family", "") for c in item.creators) or "Anon"
        print(f"  {i}. {item.year or '----'}  {authors[:28]:<28} {item.title[:64]}")
        if item.publisher:
            print(f"     {item.publisher}")

    if not 1 <= args.pick <= len(candidates):
        print(f"[error] --pick {args.pick} is outside 1..{len(candidates)}")
        return 2
    chosen = candidates[args.pick - 1]

    try:
        book = backend.get(chosen.key)
    except RuntimeError as e:
        print(f"[error] {e}")
        if len(candidates) > 1:
            print(f"        try another candidate: --pick 1..{len(candidates)}")
        return 2
    if getattr(backend, "is_shadow", False):
        note = "Z-Library copy (shadow library, not an open-access licence)"
    elif getattr(type(backend), "get_attachment", None) is not Backend.get_attachment:
        note = f"{backend.name} record (open access)"
    else:
        note = f"{backend.name} record (metadata only, no file)"
    print(f"\n{note}: {book.url}")
    for label, value in (("title", book.title), ("publisher", book.publisher),
                         ("year", book.year), ("edition", book.edition), ("isbn", book.isbn),
                         ("pages", book.page), ("language", book.extra.get("language", "")),
                         ("file", book.extra.get("download", ""))):
        if value:
            print(f"  {label}: {value}")
    if book.creators:
        print(f"  authors: {', '.join(c.get('family', '') for c in book.creators)}")
    if not args.download:
        if getattr(type(backend), "get_attachment", None) is not Backend.get_attachment:
            print("  Add --download to fetch the file.")
        return 0

    try:
        raw = backend.get_attachment(book.key)
    except NotSupportedError:
        print(f"  [{backend.name}] has no downloadable file; open {book.url} instead.")
        return 0
    out = args.out or f"{book.isbn or book.key.split('/')[-1]}.pdf"
    with open(out, "wb") as f:
        f.write(raw)
    print(f"  Downloaded {len(raw)} bytes to {out}")
    if raw and args.lines:
        text = pdfutil.extract_text(raw, max_chars=None)
        print(f"  --- {args.lines}-line preview ---\n{pdfutil.first_lines(text, args.lines)}")
    return 0


def cmd_completions(cfg, args) -> int:
    """Generate a self-contained shell completion script for ``args.shell``.

    Introspects the live parser (so the spec tracks the current CLI surface) and prints the
    rendered script to stdout; the user sources it via ``eval "$(litcli completions bash)"``.
    """
    sys.stdout.write(render_completion(args.shell, build_spec(_build_parser())))
    return 0


def cmd_backends(cfg, args) -> int:
    subcmd = getattr(args, "backends_cmd", None)
    if subcmd == "authorize":
        return cmd_backends_authorize(cfg, getattr(args, "backend", "zotero"))
    if subcmd == "status":
        return cmd_backends_status(cfg, args)
    # Bare `backends` or `backends list`: list configured backends with capabilities (read-only)
    backends = get_all_backends(cfg)
    for name, b in sorted(backends.items()):
        caps = ", ".join(sorted(b.capabilities))
        print(f"{name:<12} caps=[{caps}]")
    for name, why in sorted(config_mod.failed_backends.items()):
        if name not in backends:
            print(f"{name:<12} [unavailable] {why}")
    return 0


def _cfg_write_key(cfg: dict, name: str) -> str | None:
    """Get a backend's api_key from config (only checks presence, never prints it)."""
    bcfg = (cfg.get("backends") or {}).get(name) or {}
    k = bcfg.get("api_key")
    return k if k else None


def cmd_backends_status(cfg, args) -> int:
    """Per-backend read-only diagnostics: config / capabilities / authorization & reachability (never prints key plaintext)."""
    backends = get_all_backends(cfg)
    if not backends and not config_mod.failed_backends:
        print("[no backends] check ~/.config/litcli/config.toml (or env LITCLI_CONFIG)")
        return 0
    print(f"backend diagnostics: {len(backends)} configured")
    for name, b in sorted(backends.items()):
        caps = ", ".join(sorted(b.capabilities))
        has_write = "write" in b.capabilities
        if has_write:
            auth = "authorized (has write key)" if _cfg_write_key(cfg, name) else "unauthorized (write needs `backends authorize`)"
        else:
            auth = "read-only backend, no auth needed"
        # Lightweight reachability probe only for backends providing ping() (zotero local); non-fatal if it fails.
        probe = ""
        ping = getattr(b, "ping", None)
        if callable(ping):
            probe = "  reachable" if ping() else "  [unreachable: backend not running?]"
        print(f"  {name:<12} caps=[{caps}]  {auth}{probe}")
    for name, why in sorted(config_mod.failed_backends.items()):
        if name not in backends:
            print(f"  {name:<12} [unavailable] {why}")
    return 0


def cmd_backends_authorize(cfg, backend: str) -> int:
    """Actively authorize against local Zotero to get a key, and write it back to config.toml.

    The authorized backend must support local authorization (implement authorize()). Once
    successful (or already authorized), writes no longer prompt.
    """
    try:
        b = get_backend(backend, cfg)
    except KeyError as e:
        print(f"[error] {e}")
        return 2
    if not hasattr(b, "authorize"):
        print(f"[error] backend '{backend}' does not support local authorization (only zotero_local provides it)")
        return 2
    try:
        key = b.authorize()
    except RuntimeError as e:
        print(f"[error] {e}")
        return 2
    if not key:
        print(f"[error] {backend} authorization returned no key (built-in key may be in use, or not approved)")
        return 2
    path = write_backend_key(backend, key)
    print(f"[ok] Wrote {backend}'s write key to {path}")
    print("     Subsequent writes (data add / set / del / attach) need no further authorization.")
    return 0


def _apply_proxy(cfg: dict) -> None:
    """If config sets [network].proxy and no env var proxy is set, route all traffic through it (campus / VPN access to overseas sources)."""
    prog = (cfg.get("network") or {}).get("proxy")
    if not prog:
        return
    if os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy"):
        return  # env var already set; it takes precedence
    set_backend_proxy(prog)


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        cfg = load_config()
        _apply_proxy(cfg)
        if args.cmd == "search":
            return cmd_search(cfg, args)
        if args.cmd in _EDGE_METHODS:
            return _cmd_edge(cfg, args, _EDGE_METHODS[args.cmd])
        if args.cmd == "export":
            return cmd_export(cfg, args)
        if args.cmd == "completions":
            return cmd_completions(cfg, args)
        if args.cmd == "backends":
            return cmd_backends(cfg, args)
        if args.cmd == "data":
            return {
                "get": cmd_get, "add": cmd_data_add,
                "set": cmd_set, "del": cmd_data_del,
                "attach": cmd_data_attach,
                "tree": cmd_data_tree, "members": cmd_data_members,
            }[args.data_cmd](cfg, args)
        if args.cmd == "fetch":
            return {
                "fulltext": cmd_fetch_fulltext,
                "book": cmd_fetch_book,
            }[args.fetch_cmd](cfg, args)
        print(f"[error] unknown command {args.cmd}")
        return 2
    except NotSupportedError as e:
        print(f"[skipped] {e}")
        return 2
    except KeyError as e:
        print(f"[error] {e}")
        return 2
    except (RuntimeError, ValueError, SyntaxError, OSError) as e:
        # Backend business errors (HTTP 401/404/timeout) and data-shape failures (non-JSON response,
        # malformed XML, config TOML syntax) are surfaced gracefully, never as a raw stack trace.
        # (json.JSONDecodeError -> ValueError, ET.ParseError -> SyntaxError, TOMLDecodeError -> ValueError,
        #  URLError -> OSError.)
        print(f"[error] {e}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
