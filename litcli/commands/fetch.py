"""``fetch``: save the fulltext behind an id, or a book by title/ISBN. Read-only, nothing is stored.

One command, one target. ``fetch <doi|arxiv-id|publisher-url|isbn>`` walks the legitimate chain
(local store → Unpaywall/OpenAlex → Europe PMC/Zenodo → ScienceDirect / Wiley / Springer /
Cambridge / AIP / ACM / APS / IOP / Taylor & Francis / AIAA / IEEE when entitled → landing page) and
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
from typing import NamedTuple

from .. import config as config_mod
from .. import fulltext as fulltext_mod
from .. import pdf as pdfutil
from .. import provenance, unpaywall
from ..backends.base import Backend, NotSupportedError
from ..browser import looks_like_pdf, session_from_config
from ..config import get_all_backends, get_backend
from ..errors import LitcliError, Unavailable
from ..paths import target_path
from ..publishers import _PUBLISHER_SPECS, _PublisherSpec
from .keys import normalize_related_key

_ISBN_RE = re.compile(r"^(?:97[89][\d-]{10,17}|\d{9}[\dXx])$")

#: A target that already is a DOI (an arXiv id has been turned into one by normalization).
_DOI_LIKE_RE = re.compile(r"^10\.\d{4,9}/")


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
    """Instantiate a registered optional backend, or None when it is not available at all.

    A section that is present but cannot be built (bad cookie_file, unknown type) raises out of
    ``get_backend``; the optional steps of the chain must tolerate that and keep walking.
    """
    try:
        return get_backend(name, cfg)
    except (KeyError, RuntimeError):
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
        # mkstemp opens 0600; a downloaded paper is not a credential, so give it the usual
        # 0666 & ~umask (the rename carries the mode over).
        mask = os.umask(0)
        os.umask(mask)
        os.chmod(tmp, 0o666 & ~mask)
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


def _publisher_for_url(publishers: dict, url: str):
    """The configured publisher whose site ``url`` lives on, if any.

    An OA link resolved elsewhere (Unpaywall) often points at the publisher's own PDF endpoint,
    which answers a plain client with a challenge: it still needs that publisher's session, so
    the host decides whose session to use when the hit's kind names no backend.
    """
    for backend in publishers.values():
        matches = getattr(backend, "matches_url", None)
        if matches is not None and matches(url):
            return backend
    return None


class _Probe(NamedTuple):
    """What the probe step established: a chain answer, or nothing plus where it stopped.

    ``info is None`` means a pasted page the chain could not key on (``unresolved`` says which
    steps never ran and why); an ``info`` without ``has_fulltext`` is a clean miss, which the
    probe reports from ``pub_errors``/``errors`` rather than through a page.
    """

    key: str
    info: dict | None
    email: str
    scihub_backend: object
    publishers: dict[str, Backend]
    pub_errors: dict[str, str]
    unresolved: list[tuple[_PublisherSpec, bool]]
    pages: dict[str, str]
    #: The configured browser session, used only where a plain client is walled off; started
    #: lazily, so a run that never needs it never launches a browser.
    browser: object = None


def _fetch_fulltext(cfg, args, target: str) -> int:
    """Walk the legitimate fulltext chain for ``target`` and save the file (or print the link).

    Sequencing only: the probe step turns the target into an answer (or reports why there is
    none), the emit step renders the answer and gets the bytes. Sci-Hub is opt-in
    (``--scihub``, or by configuring the ``scihub`` backend) and stays last: it is a public copy
    without an open licence, so its hits say so.
    """
    probe = _probe_fulltext(cfg, args, target)
    if probe is None:  # a malformed target, already reported
        return 2
    try:
        if probe.info is None:
            return _report_unresolved(probe)
        if not probe.info.get("has_fulltext"):
            return _report_no_fulltext(probe)
        return _emit_fulltext(args, probe)
    finally:
        if probe.browser is not None:
            # A file left up in the tab keeps a pdf-renderer spinning after the driver is gone,
            # and its signed address dies in minutes — the desktop should not keep showing it.
            probe.browser.reset()
            probe.browser.close()


def _probe_fulltext(cfg, args, target: str) -> _Probe | None:
    """Resolve ``target`` to a chain answer, probing the publishers the target names.

    Returns ``None`` when the target is shaped like a publisher key but does not normalize
    (already reported); otherwise the answer, or the reason no step could answer.
    """
    key = normalize_related_key(target)
    # A pasted publisher URL, or a ``wiley:<DOI>`` / ``cambridge:<DOI>`` form, normalizes to the
    # DOI; a target shaped like one of those sites must normalize or it would drift down the
    # chain. The publisher whose own shape it is reports the error — so the site the URL lives
    # on is asked first: a generic ``/article/<DOI>`` shape (SpringerLink's) would otherwise
    # swallow another publisher's URL and leave a sub-page (``/pdf``) on the DOI.
    stripped = target.strip()
    owned = sorted(((bool(s.like.match(stripped)), s) for s in _PUBLISHER_SPECS),
                   key=lambda pair: not pair[0])
    for own, spec in owned:
        try:
            normalized = spec.normalize(key)
        except LitcliError as e:
            if own:
                note(f"[error] {e}")
                return None  # reported; the caller only skips to exit 2 on `None`
            continue
        if normalized:  # an empty answer means this publisher does not own the shape
            key = normalized[len("url:"):] if normalized.startswith("url:") else normalized[4:]
            break
    email = _resolve_oa_email(cfg, args)
    backends = get_all_backends(cfg)
    scihub_backend = backends.get("scihub")
    if args.scihub and scihub_backend is None:
        scihub_backend = _optional_backend("scihub", cfg)
    # Only for the plain-download fallback: a keyless OA host can still be behind a bot wall,
    # and a browser nobody ends up needing is never launched (``start`` is lazy).
    browser = session_from_config(cfg)
    # The legal corpus sources are keyless: consult them even when not enabled in config — but
    # a section the user switched off stays off.
    corpus = {name: backends.get(name) or (
        None if config_mod.section_disabled(name, cfg) else _optional_backend(name, cfg))
        for name in ("europepmc", "zenodo")}
    # A publisher step starts for its own identifiers (a pasted page, or its DOI prefix) even
    # unconfigured: an unentitled or unconfigured run still has to say *why* it stopped.
    pages: dict[str, str] = {}
    publishers: dict[str, Backend] = {}
    pub_errors: dict[str, str] = {}
    for spec in _PUBLISHER_SPECS:
        page = "" if _DOI_LIKE_RE.match(key) else spec.page_key(target)
        backend = backends.get(spec.name)
        if backend is None and (page or key.lower().startswith(spec.doi_re)):
            if config_mod.section_disabled(spec.name, cfg):
                # Say the user's own reason, and do not probe a source they switched off.
                pub_errors[spec.name] = "disabled in the config (enabled = false)"
            else:
                backend = _optional_backend(spec.name, cfg)
                if backend is None:  # a broken section must not sink the rest of the chain
                    pub_errors[spec.name] = config_mod.failed_backends.get(
                        spec.name, "backend unavailable")
        pages[spec.name] = page
        if backend is not None:
            publishers[spec.name] = backend
    info = None
    unresolved: list[tuple[_PublisherSpec, bool]] = []
    for spec in _PUBLISHER_SPECS:
        if info is not None:
            break
        page = pages[spec.name]
        if not page:
            continue
        backend = publishers.get(spec.name)
        if backend is None:
            unresolved.append((spec, False))
            continue
        # The page is the identifier the user has; its DOI (when the page carries one) lets the
        # rest of the chain run if this step is blocked.
        got_page = False
        try:
            item = backend.get(page)
        except (LitcliError, RuntimeError) as e:
            pub_errors[spec.name] = str(e)
        else:
            got_page = True
            if item.doi:
                key = item.doi
                pages[spec.name] = ""
            if item.attachments:
                info = {"has_fulltext": True, "kind": spec.name, "url": item.attachments[0],
                        "title": item.title,
                        "access": getattr(item, "access", "") or provenance.ENTITLED}
        if info is None and pages[spec.name]:
            unresolved.append((spec, got_page))
    if info is None and looks_like_pdf(stripped):
        # A file link handed over in place of a key is the target itself: re-deriving it from a
        # page would only add a hop that can fail on its own (an unentitled ``pdfft``, a signed
        # asset address that expires in minutes).
        info = {"has_fulltext": True, "direct": True, "kind": "url", "url": stripped,
                "format": "pdf"}
    if info is None and unresolved:
        # A pasted page the chain could not key on: probing every backend with a URL would not
        # help, so the report says which step is why.
        return _Probe(key, None, email, scihub_backend, publishers, pub_errors, unresolved,
                      pages, browser)
    if info is None:
        info = fulltext_mod.resolve_fulltext(key, email, backends=backends,
                                             openalex_backend=backends.get("openalex"),
                                             europepmc_backend=corpus["europepmc"],
                                             zenodo_backend=corpus["zenodo"],
                                             publishers=publishers,
                                             scihub_backend=scihub_backend)
    return _Probe(key, info, email, scihub_backend, publishers, pub_errors, unresolved,
                  pages, browser)


def _report_unresolved(probe: _Probe) -> int:
    """Report each pasted page no step could turn into a DOI, and why."""
    for spec, got_page in probe.unresolved:
        page = probe.pages[spec.name]
        if probe.publishers.get(spec.name) is None:
            note(f"{spec.label} {page[len(spec.page_prefix):]}  "
                 f"{probe.pub_errors.get(spec.name) or 'backend unavailable'}")
        else:
            note(f"{spec.label} {page[len(spec.page_prefix):]}  "
                 f"{'the item page gave no DOI' if got_page else 'the item page did not load'}")
            if probe.pub_errors.get(spec.name):
                note(f"  [skipped] {probe.pub_errors[spec.name]}")
            else:
                note(spec.hint)
    return 1


def _report_no_fulltext(probe: _Probe) -> int:
    """Report a chain that answered nothing, with the per-step reasons and the way forward."""
    note(f"DOI {probe.key}  no such work in the local store or open OA sources")
    for spec in _PUBLISHER_SPECS:
        if probe.pub_errors.get(spec.name):
            note(f"  [skipped] {probe.pub_errors[spec.name]}")
    for reason in probe.info.get("errors") or []:
        note(f"  [skipped] {reason}")
    for spec in _PUBLISHER_SPECS:
        if probe.key.lower().startswith(spec.doi_re):
            note(spec.hint)
    if not probe.scihub_backend:
        note("  add --scihub to also try Sci-Hub, or use institutional access / interlibrary loan")
    else:
        note("  use institutional access / interlibrary loan / a proxy")
    return 1


def _announce_meta(info: dict, kind: str) -> None:
    """Say what class of copy the hit is, and its title/status — before any file handling."""
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


def _announce_hit(kind: str, info: dict, fmt: str) -> None:
    """Say which step answered and with what, and warn when the answer is a page not a file."""
    note(f"  source={kind}  {'PDF' if fmt == 'pdf' else fmt.upper()}: {info['url']}")
    if fmt == "html":
        note("  note: landing page, not a file — open it in a browser")


def _download_any(probe: _Probe, url: str, fmt: str) -> bytes:
    """Get ``url``'s bytes: plain first, then through the browser when the plain way failed.

    A keyless open-access host can still sit behind a bot wall (MDPI answers Cloudflare 403 to
    every plain client, browser UA included), and the configured ``[browser]`` session is the
    only thing that gets past it. Raises when no byte came either way: the failure is the
    caller's to judge (the first caller still has the chain tail to try, the last one is the
    command's answer).
    """
    try:
        raw = pdfutil.download(url)
        _verify(raw, fmt, url)
        return raw
    except (LitcliError, OSError) as e:
        # A 200 that is a paywall / login page is not a file either: verify inside the try so
        # the browser path covers the same failures.
        note(f"  [skipped] {e}")
        plain_error = e
    if "X-Amz-Expires" in url:
        # A presigned asset address is good for minutes; a retry that reuses it is not the
        # same request as the first, and the failure then says nothing useful without this.
        note("  note: a signed address expires in minutes; a re-run mints a fresh one")
    if probe.browser is None:
        raise plain_error
    try:
        if not probe.browser.alive():
            probe.browser.start()
        raw = probe.browser.download(url)
        _verify(raw, fmt, url)
        return raw
    except (LitcliError, RuntimeError, OSError) as e:
        # The browser is the fallback: when it fails too, the plain failure is what describes
        # the host (a wall says nothing about the browser), so that is the one raised.
        note(f"  [skipped] browser: {e}")
        raise plain_error from e


def _emit_fulltext(args, probe: _Probe) -> int:
    """Render the hit, get its bytes and save them; fall back to the chain tail on a failure."""
    key, publishers = probe.key, probe.publishers
    info = probe.info
    kind = info["kind"]
    _announce_meta(info, kind)

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
    _announce_hit(kind, info, fmt)
    if args.no_download:
        out(info["url"])
        return 0
    if fmt == "html" and probe.scihub_backend is not None:
        # The user asked for a copy and allowed a public one, and a landing page is no copy:
        # stopping on it without even asking Sci-Hub reads as the flag being ignored. Only a
        # step that answers with a file displaces the page; the page still wins otherwise.
        tail = fulltext_mod.resolve_fulltext(key, probe.email, tail_only=True,
                                             scihub_backend=probe.scihub_backend)
        for reason in tail.get("errors") or []:
            note(f"  [skipped] {reason}")
        if tail.get("has_fulltext") and tail.get("url") and tail.get("format") != "html":
            note("  note: the landing page is not a file; a copy was found behind it")
            info, kind = tail, tail["kind"]
            _announce_meta(info, kind)
            fmt = info.get("format") or "pdf"
            _announce_hit(kind, info, fmt)
    if fmt == "html":
        out(info["url"])
        return 0

    raw = None
    publisher = publishers.get(kind) or _publisher_for_url(publishers, info["url"])
    # Where to send the reader when no byte comes at all (shown only if the whole chain fails).
    link_hint = f"  open the PDF in your {'entitled ' if publisher else ''}browser: {info['url']}"
    if publisher is not None:
        # The PDF needs the same entitled session as the page (and may sit behind a second
        # gate), so downloading it through a plain client would only collect a challenge page.
        try:
            raw = publisher.get_attachment(key)
        except (LitcliError, RuntimeError) as e:
            note(f"  [skipped] {e}")
            raw = None
    if raw is None and (publisher is None or info.get("direct")):
        # No session-carrying backend for this host: plain bytes, then the browser if the site
        # walls plain clients off. A failure here is not yet the answer — the tail still runs.
        try:
            raw = _download_any(probe, info["url"], fmt)
        except (LitcliError, RuntimeError, OSError):
            raw = None
    if raw is None:
        # The hit answered (or the bytes were wrong) but no file came — a link that always
        # exists, e.g. Wiley's unentitled ``pdfdirect``, or an OA link that turned into a wall.
        # The rest of the chain still deserves its turn before this is a failure.
        tail = fulltext_mod.resolve_fulltext(key, probe.email, tail_only=True,
                                             scihub_backend=probe.scihub_backend)
        for reason in tail.get("errors") or []:
            note(f"  [skipped] {reason}")
        if not (tail.get("has_fulltext") and tail.get("url")):
            note(link_hint)
            return 1
        info = tail
        kind = info["kind"]
        _announce_meta(info, kind)
        fmt = info.get("format") or "pdf"
        _announce_hit(kind, info, fmt)
        if args.no_download or fmt == "html":
            out(info["url"])
            return 0
        # Last answer in the chain: its failure is the command's answer, so the exception goes
        # on to the error mapping instead of turning into another "no file" turn.
        raw = _download_any(probe, info["url"], fmt)
    # Always verified here, including the plain-download branch above (which also verifies, so a
    # non-file never reaches the save): one place proves every saved byte.
    # UNVERIFIED edge: a local-store hit carries no ``format``, so it is checked as a PDF. A
    # local attachment that is an HTML snapshot would fail here; the parent-item path filters on
    # ``contentType == application/pdf`` (see zotero_local), so it has not been observed — no
    # real HTML-snapshot sample was run.
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
