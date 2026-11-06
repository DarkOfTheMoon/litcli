"""``fetch``: save the fulltext behind an id, or a book by title/ISBN. Read-only, nothing is stored.

One command, one target. ``fetch <doi|arxiv-id|isbn>`` walks the legitimate chain (local store →
Unpaywall/OpenAlex → Europe PMC/Zenodo → landing page) and **saves the file by default**, printing
exactly one line on stdout: the path. Everything else (what source answered, notes, previews) goes
to stderr so ``f=$(litcli fetch <id>)`` stays clean. Exit codes: 0 file saved, 1 nothing found,
2 error.
"""
from __future__ import annotations

import os
import re
import sys

from .. import fulltext as fulltext_mod
from .. import pdf as pdfutil
from .. import unpaywall
from ..backends.base import Backend, NotSupportedError
from ..config import get_all_backends, get_backend
from ..paths import target_path
from .keys import normalize_related_key

_ISBN_RE = re.compile(r"^(?:97[89][\d-]{10,17}|\d{9}[\dXx])$")


def out(text: str) -> None:
    """The machine-readable result line (path or link) — stdout."""
    print(text)


def note(text: str) -> None:
    """Diagnostics, sources and notes — stderr, so stdout stays a single value."""
    print(text, file=sys.stderr)

#: Preview extractor per fulltext format (PDF falls through to pdftotext).
_TEXT_EXTRACTORS = {"html": fulltext_mod.html_to_text, "xml": fulltext_mod.xml_to_text}


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


def _save(raw: bytes, path: str, lines: int, verb: str = "Downloaded",
          extract=pdfutil.extract_text) -> None:
    """Write the fetched bytes to ``path`` and preview its first ``lines`` lines on stderr.

    ``extract`` differs per format (PDF text via pdftotext, JATS XML, HTML); each takes the
    bytes and returns text.
    """
    with open(path, "wb") as f:
        f.write(raw)
    note(f"  {verb} {len(raw)} bytes to {path}")
    if raw and lines:
        text = extract(raw)
        note(f"  --- {lines}-line preview ---\n{pdfutil.first_lines(text, lines)}")


def cmd_fetch(cfg, args) -> int:
    """Fetch ``args.target``: a book when ``--book`` or the target is an ISBN, else a paper fulltext."""
    target = (args.target or "").strip()
    if not target:
        note("[error] empty target")
        return 2
    if args.book or _ISBN_RE.match(target):
        return _fetch_book(cfg, args, target)
    return _fetch_fulltext(cfg, args, target)


def _fetch_fulltext(cfg, args, target: str) -> int:
    """Walk the legitimate fulltext chain for ``target`` and save the file (or print the link).

    Sci-Hub is opt-in (``--scihub``, or by configuring the ``scihub`` backend) because it serves
    copyright-evading copies; its hits are labelled as such.
    """
    key = normalize_related_key(target)
    email = _resolve_oa_email(cfg, args)
    backends = get_all_backends(cfg)
    scihub_backend = backends.get("scihub")
    if args.scihub and scihub_backend is None:
        scihub_backend = _optional_backend("scihub", cfg)
    # The legal corpus sources are keyless: consult them even when not enabled in config.
    corpus = {name: backends.get(name) or _optional_backend(name, cfg)
              for name in ("europepmc", "zenodo")}
    info = fulltext_mod.resolve_fulltext(key, email, backends=backends,
                                         openalex_backend=backends.get("openalex"),
                                         europepmc_backend=corpus["europepmc"],
                                         zenodo_backend=corpus["zenodo"],
                                         scihub_backend=scihub_backend)
    if not info.get("has_fulltext"):
        note(f"DOI {key}  no such work in the local store or open OA sources")
        if not scihub_backend:
            note("  add --scihub to also try Sci-Hub, or use institutional access / interlibrary loan")
        else:
            note("  use institutional access / interlibrary loan / a proxy")
        return 1

    kind = info["kind"]
    if kind == "scihub":
        note("  note: Sci-Hub copy (copyright-evading), not an open-access licence")
    if info.get("format") == "xml":
        note("  note: open-access JATS XML (text without a PDF; not a rendered article)")
    if info.get("title"):
        note(f"  title: {info['title'][:70]}")
    if info.get("status"):
        note(f"  oa_status: {info['status']}")

    if kind == "local":  # local-store hit: the attachment is already in hand
        note(f"  local store [{info.get('source') or 'zotero'}] attachment matched (key={info.get('key')})")
        path = target_path(args.out, key, "pdf")
        _save(info["data"], path, args.lines, verb="Saved")
        out(path)
        return 0

    fmt = info.get("format") or "pdf"
    note(f"  source={kind}  {'PDF' if fmt == 'pdf' else fmt.upper()}: {info['url']}")
    if fmt == "html":
        note("  note: landing page, not a file — open it in a browser")
    if args.no_download or fmt == "html":
        out(info["url"])
        return 0

    path = target_path(args.out, key, fmt)
    _save(pdfutil.download(info["url"]), path, args.lines,
          extract=_TEXT_EXTRACTORS.get(fmt, pdfutil.extract_text))
    out(path)
    return 0


def _fetch_book(cfg, args, target: str) -> int:
    """Resolve ``target`` on a book backend and save the file.

    Z-Library copies are shadow-library copies (not open-access licensed), so the hit is
    labelled as such. ``--pick`` selects among the listed candidates; the chosen record is
    printed before any bytes are fetched.
    """
    backend = get_backend(args.backend or "zlibrary", cfg)
    candidates = backend.search(target, limit=max(1, args.limit))
    if not candidates:
        note(f"[no books] {target!r}")
        return 1

    note(f"{len(candidates)} candidate(s) from {backend.name}:")
    for i, item in enumerate(candidates, 1):
        authors = ", ".join(c.get("family", "") for c in item.creators) or "Anon"
        note(f"  {i}. {item.year or '----'}  {authors[:28]:<28} {item.title[:64]}")
        if item.publisher:
            note(f"     {item.publisher}")

    if not 1 <= args.pick <= len(candidates):
        note(f"[error] --pick {args.pick} is outside 1..{len(candidates)}")
        return 2
    chosen = candidates[args.pick - 1]

    try:
        book = backend.get(chosen.key)
    except RuntimeError as e:
        note(f"[error] {e}")
        if len(candidates) > 1:
            note(f"        try another candidate: --pick 1..{len(candidates)}")
        return 2
    if getattr(backend, "is_shadow", False):
        label = "Z-Library copy (shadow library, not an open-access licence)"
    elif getattr(type(backend), "get_attachment", None) is not Backend.get_attachment:
        label = f"{backend.name} record (open access)"
    else:
        label = f"{backend.name} record (metadata only, no file)"
    note(f"  {label}: {book.url}")
    for name, value in (("title", book.title), ("publisher", book.publisher),
                        ("year", book.year), ("edition", book.edition), ("isbn", book.isbn),
                        ("pages", book.page)):
        if value:
            note(f"  {name}: {value}")
    if book.creators:
        note(f"  authors: {', '.join(c.get('family', '') for c in book.creators)}")

    can_download = callable(getattr(backend, "get_attachment", None)) \
        and getattr(type(backend), "get_attachment", None) is not Backend.get_attachment
    if not can_download or args.no_download:
        out(book.url)
        return 0

    try:
        raw = backend.get_attachment(book.key)
    except NotSupportedError:
        note(f"  [{backend.name}] has no downloadable file; open {book.url} instead")
        out(book.url)
        return 1
    path = target_path(args.out, book.isbn or book.key.split("/")[-1], "pdf")
    _save(raw, path, args.lines)
    out(path)
    return 0
