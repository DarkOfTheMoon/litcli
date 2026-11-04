"""The argparse surface: intent-based subcommands, dispatched by ``litcli.cli.main``.

Each ``--backend`` defaults to the backend that actually serves the intent (zotero for the
local store, openalex for the graph edges, zlibrary for books).
"""
from __future__ import annotations

import argparse

from .. import __version__
from ..completions import SHELL_CHOICES


def add_common_backend(p: argparse.ArgumentParser, default: str = "zotero") -> None:
    p.add_argument("--backend", default=default,
                   help=f"backend name (default {default})")


def build_parser() -> argparse.ArgumentParser:
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
                        help="sort descending by citation count (refs/citing are already citation-desc; "
                             "this flag applies to related)")
        pg.add_argument("--no-cite", action="store_true", help="do not show in-text citations in table mode")

    p = sub.add_parser("export", help="export citations (read-only, no PDF)")
    p.add_argument("key", nargs="+", help="one or more item keys")
    add_common_backend(p)
    p.add_argument("--format", choices=["csljson", "bibtex", "ris", "text"], default="csljson")

    cp = sub.add_parser("completions", help="generate shell completion scripts (enable via eval; bash/zsh/fish)")
    cp.add_argument("shell", choices=SHELL_CHOICES, help="target shell")

    p = sub.add_parser("backends", help="backend diagnostics / authorization "
                                        "(get a write key via `backends authorize` before writing)")
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
    add_common_backend(dp)
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
    dp.add_argument("--yes", action="store_true",
                    help="skip the interactive confirmation prompt (for --pdf; --force already implies this)")
    add_common_backend(dp)

    dp = dsub.add_parser("set", help="modify item fields")
    dp.add_argument("key")
    dp.add_argument("--title", default=None)
    dp.add_argument("--abstract", default=None)
    dp.add_argument("--year", default=None)
    dp.add_argument("--doi", default=None)
    dp.add_argument("--url", default=None)
    add_common_backend(dp)

    dp = dsub.add_parser("del", help="delete an item")
    dp.add_argument("key")
    add_common_backend(dp)

    dp = dsub.add_parser("attach", help="attach a fulltext file to an existing item")
    dp.add_argument("key")
    dp.add_argument("src", help="local path or URL")
    add_common_backend(dp)

    dp = dsub.add_parser("tree", help="list the collection tree (hierarchical display; read-only)")
    add_common_backend(dp)

    dp = dsub.add_parser("members", help="list items in a collection (filtered by collection; read-only)")
    dp.add_argument("coll_key", help="collection key (see it via `data tree`)")
    dp.add_argument("--no-recursive", action="store_true", default=False,
                    help="only that collection itself, without recursing into subcollections")
    add_common_backend(dp)
    dp.add_argument("--format", choices=["table", "json"], default="table")
    dp.add_argument("--limit", type=int, default=200, help="max items to list (0=unlimited)")

    # --- fetch: get fulltext / metadata (read-only, no store) ---
    f = sub.add_parser("fetch", help="get fulltext / metadata (read-only, no store)")
    fsub = f.add_subparsers(dest="fetch_cmd", required=True)

    fp = fsub.add_parser("fulltext",
                         help="locate fulltext by DOI (local store → Unpaywall → OpenAlex → Europe PMC/Zenodo; "
                              "--scihub adds a fallback)")
    fp.add_argument("doi")
    fp.add_argument("--email", default=None)
    fp.add_argument("--download", action="store_true",
                    help="download the candidate fulltext file (PDF, or XML/HTML from the OA corpus)")
    fp.add_argument("--out", default=None, help="save path")
    fp.add_argument("--lines", type=int, default=6, help="preview line count after download")
    fp.add_argument("--scihub", action="store_true",
                    help="also try Sci-Hub after the legitimate OA sources (copyright-evading mirror)")

    bp = fsub.add_parser("book", help="search a book by title/ISBN on a book backend (default Z-Library) "
                                     "and resolve one hit")
    bp.add_argument("query", help="title, author or ISBN")
    bp.add_argument("--backend", default="zlibrary", help="book backend (default zlibrary)")
    bp.add_argument("--limit", type=int, default=5, help="candidate count to list")
    bp.add_argument("--pick", type=int, default=1, help="1-based candidate to resolve (default 1)")
    bp.add_argument("--download", action="store_true", help="download the chosen file")
    bp.add_argument("--out", default=None, help="save path")
    bp.add_argument("--lines", type=int, default=0, help="preview line count after download (0=off)")

    return parser
