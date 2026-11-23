"""``fetch``: save the fulltext behind an id, or a book by title/ISBN. Read-only, nothing is stored.

One command, one target. ``fetch <doi|arxiv-id|sciencedirect-url|isbn>`` walks the legitimate chain (local store →
Unpaywall/OpenAlex → Europe PMC/Zenodo → ScienceDirect when entitled → landing page) and
**saves the file by default**, printing
exactly one line on stdout: the path. Everything else (what source answered, notes, previews) goes
to stderr so ``f=$(litcli fetch <id>)`` stays clean. Exit codes: 0 file saved, 1 nothing found,
2 error.
"""
from __future__ import annotations

import contextlib
import os
import re
import sys
import tempfile

from .. import fulltext as fulltext_mod
from .. import pdf as pdfutil
from .. import provenance, unpaywall
from ..backends.base import Backend, NotSupportedError
from ..backends.sciencedirect import normalize_key as sciencedirect_key
from ..backends.wiley import normalize_key as wiley_key
from ..config import get_all_backends, get_backend
from ..errors import LitcliError, Unavailable
from ..paths import target_path
from .keys import normalize_related_key

_ISBN_RE = re.compile(r"^(?:97[89][\d-]{10,17}|\d{9}[\dXx])$")

#: DOI prefixes owned by Elsevier (ScienceDirect, incl. the 10.1006/10.1053/10.1067 legacy ones).
ELSEVIER_DOI_RE = ("10.1016/", "10.1006/", "10.1053/", "10.1067/")

#: DOI prefixes owned by Wiley (the Online Library, incl. the 10.1046/10.1096 legacy ones and
#: the Physiological Society's 10.1113/10.1114).
WILEY_DOI_RE = ("10.1002/", "10.1111/", "10.1113/", "10.1114/", "10.1046/", "10.1096/")

#: Targets that are Wiley by shape: a ``wiley:`` key, or a Wiley / doi.org URL.
_WILEY_LIKE_RE = re.compile(r"^(?:wiley:|https?://(?:[\w-]+\.)*(?:wiley\.com|doi\.org)/)", re.I)

#: A target that already is a DOI (an arXiv id has been turned into one by normalization).
_DOI_LIKE_RE = re.compile(r"^10\.\d{4,9}/")


def _sciencedirect_key(target: str) -> str:
    """``pii:<ID>`` when the target is a ScienceDirect article URL or bare PII, else ``""``."""
    try:
        normalized = sciencedirect_key(target)
    except LitcliError:
        return ""
    return normalized if normalized.startswith("pii:") else ""


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


def _verify(raw: bytes, fmt: str, url: str) -> None:
    """Refuse to call a 200 a file when the bytes are a paywall/error page.

    Only the local store and zlibrary/zenodo used to check; the OA paths would happily save a
    login wall as ``<key>.pdf`` and exit 0.
    """
    if fmt == "pdf" and not raw.startswith(b"%PDF"):
        head = raw[:120].decode("utf-8", "replace").replace("\n", " ").strip()
        raise Unavailable(f"not a PDF from {url} (starts: {head!r})")
    if fmt == "xml" and not raw.lstrip()[:1] == b"<":
        raise Unavailable(f"not XML from {url}")


def _save(raw: bytes, path: str, lines: int, verb: str = "Downloaded",
          extract=pdfutil.extract_text) -> None:
    """Write the fetched bytes to ``path`` and preview its first ``lines`` lines on stderr.

    ``extract`` differs per format (PDF text via pdftotext, JATS XML, HTML); each takes the
    bytes and returns text.
    """
    # Write beside the target and rename: a reader never sees a half file, and a failed write
    # leaves no truncated ``.pdf`` behind. ``mkstemp`` opens with O_EXCL and a unique name.
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=f".{os.path.basename(path)}.", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    note(f"  {verb} {len(raw)} bytes to {path}")
    if raw and lines:
        text = extract(raw)
        note(f"  --- {lines}-line preview ---\n{pdfutil.first_lines(text, lines)}")


#: The 0.1 subcommands, still accepted in front of the target (``fetch fulltext <doi>``).
_LEGACY_VERBS = {"fulltext": "", "book": "book"}


def cmd_fetch(cfg, args) -> int:
    """Fetch ``args.target``: a book when ``--book`` or the target is an ISBN, else a paper fulltext.

    A leading ``fulltext`` / ``book`` is taken as the 0.1 verb rather than part of the target, and
    the remaining words are rejoined so an unquoted title works.
    """
    parts = [args.target or "", *(getattr(args, "extra", None) or [])]
    verb = _LEGACY_VERBS.get(parts[0].strip().lower())
    if verb is not None:
        parts = parts[1:]
        if verb == "book":
            args.book = True
    target = " ".join(p.strip() for p in parts if p and p.strip())
    if not target:
        note("[error] empty target")
        return 2
    if args.book or _ISBN_RE.match(target):
        return _fetch_book(cfg, args, target)
    return _fetch_fulltext(cfg, args, target)


def _sciencedirect_hint() -> None:
    note("  ScienceDirect needs a logged-in session: set [backends.sciencedirect] cookie"
         " (or cookie_file) from your browser's off-campus session")


def _wiley_hint() -> None:
    note("  Wiley needs a logged-in session: set [backends.wiley] cookie"
         " (or cookie_file) and user_agent from your browser's off-campus session,"
         " or set a free tdm_token (its PDF route skips Cloudflare)")


def _fetch_fulltext(cfg, args, target: str) -> int:
    """Walk the legitimate fulltext chain for ``target`` and save the file (or print the link).

    Sci-Hub is opt-in (``--scihub``, or by configuring the ``scihub`` backend) and stays last:
    it is a public copy without an open licence, so its hits say so.
    """
    key = normalize_related_key(target)
    try:
        key = wiley_key(key)[4:]  # a pasted Wiley URL / wiley:<DOI> form normalizes to the DOI
    except LitcliError as e:
        if _WILEY_LIKE_RE.match(target.strip()):  # a Wiley-shaped target must normalize
            note(f"[error] {e}")
            return 2
    email = _resolve_oa_email(cfg, args)
    backends = get_all_backends(cfg)
    scihub_backend = backends.get("scihub")
    if args.scihub and scihub_backend is None:
        scihub_backend = _optional_backend("scihub", cfg)
    # The legal corpus sources are keyless: consult them even when not enabled in config.
    corpus = {name: backends.get(name) or _optional_backend(name, cfg)
              for name in ("europepmc", "zenodo")}
    # ScienceDirect is worth a probe for its own identifiers (a pasted article URL or PII) and
    # for Elsevier DOIs, even unconfigured; other publishers would only 403 or lack a pdfUrl.
    pii_key = "" if _DOI_LIKE_RE.match(key) else _sciencedirect_key(target)
    sciencedirect_backend = backends.get("sciencedirect")
    sd_error = ""
    if sciencedirect_backend is None and (pii_key or key.lower().startswith(ELSEVIER_DOI_RE)):
        try:
            sciencedirect_backend = _optional_backend("sciencedirect", cfg)
        except RuntimeError as e:  # a broken section must not sink the rest of the chain
            sd_error = str(e)
    wiley_backend = backends.get("wiley")
    wiley_error = ""
    if wiley_backend is None and key.lower().startswith(WILEY_DOI_RE):
        try:
            wiley_backend = _optional_backend("wiley", cfg)
        except RuntimeError as e:  # a broken section must not sink the rest of the chain
            wiley_error = str(e)
    info = None
    if pii_key and sciencedirect_backend is not None:
        # The URL/PII is the identifier the user has; its DOI (when the page carries one)
        # lets the rest of the chain run if this step is blocked.
        try:
            item = sciencedirect_backend.get(pii_key)
        except (LitcliError, RuntimeError) as e:
            sd_error = str(e)
        else:
            if item.doi:
                key = item.doi
            if item.attachments:
                info = {"has_fulltext": True, "kind": "sciencedirect",
                        "url": item.attachments[0], "title": item.title, "access": "entitled"}
    if info is None and pii_key and not _DOI_LIKE_RE.match(key):
        # A ScienceDirect URL/PII whose page never gave a DOI: the chain has nothing to key
        # on, so report the gate instead of probing every backend with a URL.
        if sciencedirect_backend is None:
            note(f"ScienceDirect {pii_key}  {sd_error or 'backend unavailable'}")
        else:
            note(f"ScienceDirect {pii_key}  the article page did not load and gave no DOI")
            if sd_error:
                note(f"  [skipped] {sd_error}")
            else:
                _sciencedirect_hint()
        return 1
    if info is None:
        info = fulltext_mod.resolve_fulltext(key, email, backends=backends,
                                             openalex_backend=backends.get("openalex"),
                                             europepmc_backend=corpus["europepmc"],
                                             zenodo_backend=corpus["zenodo"],
                                             sciencedirect_backend=sciencedirect_backend,
                                             wiley_backend=wiley_backend,
                                             scihub_backend=scihub_backend)
    if not info.get("has_fulltext"):
        note(f"DOI {key}  no such work in the local store or open OA sources")
        if sd_error:
            note(f"  [skipped] {sd_error}")
        if wiley_error:
            note(f"  [skipped] {wiley_error}")
        for reason in info.get("errors") or []:
            note(f"  [skipped] {reason}")
        if sciencedirect_backend is not None and (pii_key or key.lower().startswith(ELSEVIER_DOI_RE)):
            _sciencedirect_hint()
        if wiley_backend is not None and key.lower().startswith(WILEY_DOI_RE):
            _wiley_hint()
        if not scihub_backend:
            note("  add --scihub to also try Sci-Hub, or use institutional access / interlibrary loan")
        else:
            note("  use institutional access / interlibrary loan / a proxy")
        return 1

    kind = info["kind"]
    access = info.get("access", "")
    if access == provenance.PUBLIC:
        note(f"  note: {kind} copy — public copy, not an open-access licence")
    elif access == provenance.ENTITLED:
        note("  note: subscription copy via your institution, not an open-access licence")
    if info.get("format") == "xml":
        note("  note: open-access JATS XML (text without a PDF; not a rendered article)")
    if info.get("title"):
        note(f"  title: {info['title'][:70]}")
    if info.get("status"):
        note(f"  oa_status: {info['status']}")

    if info.get("data") is not None:  # bytes in hand: local store, or an attachment backend
        source = info.get("source") or kind
        local = info.get("access") == "local"
        label = (f"local store [{source}] attachment matched (key={info.get('key')})" if local
                 else f"{source} attachment (key={info.get('key')})")
        note(f"  {label}")
        if args.no_download and info.get("url"):
            out(info["url"])
            return 0
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

    raw = None
    publisher = {"sciencedirect": sciencedirect_backend, "wiley": wiley_backend}.get(kind)
    if publisher is not None:
        # The PDF needs the same entitled session as the page (and may sit behind a second
        # gate), so downloading it through a plain client would only collect a challenge page.
        try:
            raw = publisher.get_attachment(key)
        except (LitcliError, RuntimeError) as e:
            note(f"  [skipped] {e}")
            # The publisher page answered (hence the chain stopped there) but the bytes did
            # not come — a link that always exists, e.g. Wiley's unentitled ``pdfdirect``.
            # The rest of the chain still deserves its turn before this is a failure.
            tail = fulltext_mod.resolve_fulltext(key, email, tail_only=True,
                                                 scihub_backend=scihub_backend)
            hit = tail.get("has_fulltext") and tail.get("url")
            for reason in tail.get("errors") or []:
                note(f"  [skipped] {reason}")
            if not hit:
                note(f"  open the PDF in your entitled browser: {info['url']}")
                return 1
            info = tail
            kind = info["kind"]
            fmt = info.get("format") or "pdf"
            if info.get("access") == provenance.PUBLIC:
                note(f"  note: {kind} copy — public copy, not an open-access licence")
            if info.get("title"):
                note(f"  title: {info['title'][:70]}")
            note(f"  source={kind}  {'PDF' if fmt == 'pdf' else fmt.upper()}: {info['url']}")
            if fmt == "html":
                note("  note: landing page, not a file — open it in a browser")
            if args.no_download or fmt == "html":
                out(info["url"])
                return 0
    if raw is None:
        raw = pdfutil.download(info["url"])
    _verify(raw, fmt, info["url"])
    path = target_path(args.out, key, fmt)
    _save(raw, path, args.lines, extract=_TEXT_EXTRACTORS.get(fmt, pdfutil.extract_text))
    out(path)
    return 0


def _fetch_book(cfg, args, target: str) -> int:
    """Resolve ``target`` on a book backend and save the file.

    ``--pick`` selects among the listed candidates; the chosen record is printed, with what
    kind of copy the backend serves, before any bytes are fetched.
    """
    backend = get_backend(args.backend or "zlibrary", cfg)
    candidates = backend.search(target, limit=max(1, args.limit))
    if not candidates:
        note(f"[no books] {target!r}")
        return 1

    note(f"{len(candidates)} candidate(s) from {backend.name}"
         f"{provenance.tag(getattr(backend, 'access', ''))}:")
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
    except (LitcliError, RuntimeError) as e:
        note(f"[error] {e}")
        if len(candidates) > 1:
            note(f"        try another candidate: --pick 1..{len(candidates)}")
        return 2
    access = getattr(backend, "access", "")
    if access == provenance.PUBLIC:
        label = f"{backend.name} copy — public copy, not an open-access licence"
    elif access and getattr(type(backend), "get_attachment", None) is not Backend.get_attachment:
        label = f"{backend.name} record ({provenance.label(access)})"
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
