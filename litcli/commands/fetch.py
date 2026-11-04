"""``fetch``: fulltext by DOI and books by title/ISBN. Read-only, nothing is stored."""
from __future__ import annotations

import os

from .. import fulltext as fulltext_mod
from .. import pdf as pdfutil
from .. import unpaywall
from ..backends.base import Backend, NotSupportedError
from ..config import get_all_backends, get_backend

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


def _save(raw: bytes, out: str, lines: int, verb: str = "Downloaded",
          extract=pdfutil.extract_text) -> None:
    """Write the fetched bytes to ``out`` and preview the first ``lines`` lines of text.

    ``extract`` differs per format (PDF text via pdftotext, JATS XML, HTML); each takes the
    bytes and returns text.
    """
    with open(out, "wb") as f:
        f.write(raw)
    print(f"  {verb} {len(raw)} bytes to {out}")
    if raw and lines:
        text = extract(raw)
        print(f"  --- {lines}-line preview ---\n{pdfutil.first_lines(text, lines)}")


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
        out = args.out or f"{args.doi.replace('/', '_')}.pdf"
        _save(info["data"], out, args.lines, verb="Saved")
        return 0

    # OA sources: give the direct link by default, download only with --download
    if info.get("status"):
        print(f"  oa_status: {info['status']}")
    fmt = info.get("format") or "pdf"
    print(f"  {'PDF' if fmt == 'pdf' else fmt.upper()}: {info['url']}")
    if fmt == "html":
        print("  note: landing page, not a file — open it in a browser")
    if args.download:
        out = args.out or f"{args.doi.replace('/', '_')}.{fmt}"
        _save(pdfutil.download(info["url"]), out, args.lines,
              extract=_TEXT_EXTRACTORS.get(fmt, pdfutil.extract_text))
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
    _save(raw, out, args.lines)
    return 0
