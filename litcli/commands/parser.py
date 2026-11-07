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
    parser.add_argument("--debug", action="store_true",
                        help="trace requests, pacing and retries to stderr (also LITCLI_DEBUG=1)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # --- Top-level: search / export / diagnostics ---
    p = sub.add_parser("search", help="search every enabled backend (read-only); --backend narrows to one")
    p.add_argument("query")
    only = p.add_mutually_exclusive_group()
    only.add_argument("--backend", default=None,
                      help="search this backend only (default: all enabled backends + keyless OA corpora)")
    only.add_argument("--all", action="store_true", help="explicit form of the default (all backends)")
    p.add_argument("--limit", type=int, default=10, help="max results overall (default 10)")
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
    bi = bsub.add_parser("install-browser",
                         help="download the Chromium that cnki/douban drive (needs the playwright package)")
    bi.add_argument("browser", nargs="?", default="chromium", choices=["chromium", "firefox", "webkit"],
                    help="browser to download (default chromium)")
    bi.add_argument("--check", action="store_true",
                    help="don't download; launch the browser headless and report its version")
    bi.add_argument("--dry-run", action="store_true",
                    help="show what would be downloaded (passes --dry-run to the driver)")
    bi.add_argument("--with-deps", action="store_true",
                    help="also apt-install the OS libraries the browser needs (Debian/Ubuntu)")

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

    # --- fetch: get the file behind an id (read-only, nothing is stored) ---
    f = sub.add_parser("fetch", help="save the fulltext for a DOI / arXiv id / ISBN (read-only)")
    f.add_argument("target", help="DOI, arXiv id (arxiv/1806.09180), or ISBN; a title with --book")
    f.add_argument("--book", action="store_true",
                   help="treat the target as a book title/ISBN and use a book backend")
    f.add_argument("--backend", default=None,
                   help="book backend (default zlibrary; ignored without --book)")
    f.add_argument("--no-download", action="store_true", help="print the link instead of saving the file")
    f.add_argument("--out", default=None,
                   help="save path or directory (default: <key>.<ext> in the current directory)")
    f.add_argument("--lines", type=int, default=0, help="print that many leading text lines as a preview")
    f.add_argument("--email", default=None, help="contact e-mail for the Unpaywall/OpenAlex polite pools")
    f.add_argument("--scihub", action="store_true",
                   help="also try Sci-Hub after the legitimate OA sources (copyright-evading mirror)")
    f.add_argument("--limit", type=int, default=5, help="book candidates to list (with --book)")
    f.add_argument("--pick", type=int, default=1, help="1-based book candidate to resolve (default 1)")

    return parser
