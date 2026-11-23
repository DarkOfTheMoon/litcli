"""``fetch``: save the fulltext behind an id, or a book by title/ISBN. Read-only, nothing is stored.

One command, one target. ``fetch <doi|arxiv-id|publisher-url|isbn>`` walks the legitimate chain
(local store → Unpaywall/OpenAlex → Europe PMC/Zenodo → ScienceDirect / Wiley / Springer /
Cambridge / AIP when entitled → landing page) and
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
from collections.abc import Callable
from typing import NamedTuple

from .. import fulltext as fulltext_mod
from .. import pdf as pdfutil
from .. import provenance, unpaywall
from ..backends.acm import normalize_key as acm_key
from ..backends.aiaa import normalize_key as aiaa_key
from ..backends.aip import normalize_key as aip_key
from ..backends.aps import normalize_key as aps_key
from ..backends.base import Backend, NotSupportedError
from ..backends.cambridge import normalize_key as cambridge_key
from ..backends.ieee import normalize_key as ieee_key
from ..backends.iop import normalize_key as iop_key
from ..backends.sciencedirect import normalize_key as sciencedirect_key
from ..backends.springer import normalize_key as springer_key
from ..backends.tandf import normalize_key as tandf_key
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

#: DOI prefixes owned by Springer Nature (SpringerLink, BMC/SpringerOpen, EPJ, Nature-branded
#: content, and the Palgrave/Materials & Structures legacies).
SPRINGER_DOI_RE = ("10.1007/", "10.1186/", "10.1140/", "10.1038/", "10.1617/", "10.1057/")

#: Targets that are Springer by shape: a ``springer:`` key, or a SpringerLink / Nature URL.
_SPRINGER_LIKE_RE = re.compile(
    r"^(?:springer[:\/]|https?://(?:[\w-]+\.)*(?:springer\.com|springernature\.com|nature\.com|"
    r"biomedcentral\.com|doi\.org)/)", re.I)

#: DOI prefix owned by Cambridge University Press (Cambridge Core journals and books).
CAMBRIDGE_DOI_RE = ("10.1017/",)

#: DOI prefixes hosted on pubs.aip.org (AIP Publishing itself and the Acoustical Society of
#: America, whose journals AIP serves).
AIP_DOI_RE = ("10.1063/", "10.1121/")

#: Targets that are AIP by shape: an ``aip:`` key, or a pubs.aip.org / aip.scitation.org URL.
_AIP_LIKE_RE = re.compile(
    r"^(?:aip[:/]|https?://(?:[\w-]+\.)*(?:pubs\.aip\.org|aip\.scitation\.org)/)", re.I)

#: Targets that are Cambridge by shape: a ``cambridge:`` key, or a Cambridge Core URL.
_CAMBRIDGE_LIKE_RE = re.compile(r"^(?:cambridge[:\/]|https?://(?:[\w-]+\.)*cambridge\.org/)", re.I)

#: DOI prefixes owned by the guarded publishers added in 2026-11 (ACM, APS, IOP, Taylor &
#: Francis, AIAA, IEEE Xplore). Each sits behind a bot challenge, so its step only starts for
#: its own prefixes or a pasted page URL.
ACM_DOI_RE = ("10.1145/",)
APS_DOI_RE = ("10.1103/",)
IOP_DOI_RE = ("10.1088/",)
TANDF_DOI_RE = ("10.1080/",)
AIAA_DOI_RE = ("10.2514/",)
IEEE_DOI_RE = ("10.1109/",)

_SCIENCEDIRECT_LIKE_RE = re.compile(
    r"^(?:sciencedirect[\:/]|https?://(?:[\w-]+\.)*sciencedirect\.com/)", re.I)
_ACM_LIKE_RE = re.compile(r"^(?:acm[:\/]|https?://(?:[\w-]+\.)*dl\.acm\.org/)", re.I)
_APS_LIKE_RE = re.compile(r"^(?:aps[:\/]|https?://(?:[\w-]+\.)*link\.aps\.org/)", re.I)
_IOP_LIKE_RE = re.compile(r"^(?:iop[:\/]|https?://(?:[\w-]+\.)*iopscience\.iop\.org/)", re.I)
_TANDF_LIKE_RE = re.compile(r"^(?:tandf[:\/]|https?://(?:[\w-]+\.)*tandfonline\.com/)", re.I)
_AIAA_LIKE_RE = re.compile(r"^(?:aiaa[:\/]|https?://(?:[\w-]+\.)*arc\.aiaa\.org/)", re.I)
_IEEE_LIKE_RE = re.compile(r"^(?:ieee[:\/]|https?://(?:[\w-]+\.)*ieeexplore\.ieee\.org/)", re.I)

#: A target that already is a DOI (an arXiv id has been turned into one by normalization).
_DOI_LIKE_RE = re.compile(r"^10\.\d{4,9}/")


def _sciencedirect_key(target: str) -> str:
    """``pii:<ID>`` when the target is a ScienceDirect article URL or bare PII, else ``""``."""
    try:
        normalized = sciencedirect_key(target)
    except LitcliError:
        return ""
    return normalized if normalized.startswith("pii:") else ""


class _PublisherSpec(NamedTuple):
    """One guarded publisher step: how its targets are recognised and how it is reported."""

    name: str
    label: str
    normalize: Callable[[str], str]
    like: re.Pattern
    doi_re: tuple[str, ...]
    hint: Callable[[], None]
    #: The key kind a pasted page URL yields (``pii:`` on ScienceDirect, else ``url:``); the
    #: page step is what turns it into a DOI the rest of the chain can key on.
    page_prefix: str = "url:"

    def page_key(self, target: str) -> str:
        try:
            normalized = self.normalize(target)
        except LitcliError:
            return ""
        return normalized if normalized.startswith(self.page_prefix) else ""


def out(text: str) -> None:
    """The machine-readable result line (path or link) — stdout."""
    print(text)


def note(text: str) -> None:
    """Diagnostics, sources and notes — stderr, so stdout stays a single value."""
    print(text, file=sys.stderr)

def _acm_hint() -> None:
    note("  ACM is behind a Cloudflare challenge: set [backends.acm] cookie"
         " (or cookie_file) and user_agent from a browser tab that cleared it")


def _aps_hint() -> None:
    note("  APS is behind a Cloudflare challenge: set [backends.aps] cookie"
         " (or cookie_file) and user_agent from a browser tab that cleared it")


def _iop_hint() -> None:
    note("  IOPscience is behind a Cloudflare challenge: set [backends.iop] cookie"
         " (or cookie_file) and user_agent from a browser tab that cleared it"
         " (the gate covers open-access items too)")


def _tandf_hint() -> None:
    note("  Taylor & Francis is behind a Cloudflare challenge: set [backends.tandf] cookie"
         " (or cookie_file) and user_agent from a browser tab that cleared it")


def _aiaa_hint() -> None:
    note("  AIAA is behind a Cloudflare challenge: set [backends.aiaa] cookie"
         " (or cookie_file) and user_agent from a browser tab that cleared it")


def _ieee_hint() -> None:
    note("  IEEE Xplore is behind an AWS WAF challenge: set [backends.ieee] cookie"
         " (or cookie_file) and user_agent from a browser tab that passed it")


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


def _springer_hint() -> None:
    note("  SpringerLink PDFs need a session: set [backends.springer] cookie"
         " (or cookie_file) and user_agent from an authenticated browser tab"
         " (open-access articles download without one)")


def _cambridge_hint() -> None:
    note("  Cambridge Core PDFs need a session: set [backends.cambridge] cookie"
         " (or cookie_file) and user_agent from an authenticated browser tab"
         " (open-access articles download without one)")


def _aip_hint() -> None:
    note("  AIP is behind a Cloudflare challenge: set [backends.aip] cookie"
         " (or cookie_file) and user_agent from a browser tab that cleared it")


#: Every guarded/entitled publisher, in chain order (a subscription copy outranks a page).
_PUBLISHER_SPECS = (
    _PublisherSpec("sciencedirect", "ScienceDirect", _sciencedirect_key, _SCIENCEDIRECT_LIKE_RE,
                   ELSEVIER_DOI_RE, _sciencedirect_hint, page_prefix="pii:"),
    _PublisherSpec("wiley", "Wiley", wiley_key, _WILEY_LIKE_RE, WILEY_DOI_RE, _wiley_hint),
    _PublisherSpec("springer", "SpringerLink", springer_key, _SPRINGER_LIKE_RE, SPRINGER_DOI_RE,
                   _springer_hint),
    _PublisherSpec("cambridge", "Cambridge Core", cambridge_key, _CAMBRIDGE_LIKE_RE,
                   CAMBRIDGE_DOI_RE, _cambridge_hint),
    _PublisherSpec("aip", "AIP", aip_key, _AIP_LIKE_RE, AIP_DOI_RE, _aip_hint),
    _PublisherSpec("acm", "ACM Digital Library", acm_key, _ACM_LIKE_RE, ACM_DOI_RE, _acm_hint),
    _PublisherSpec("aps", "APS", aps_key, _APS_LIKE_RE, APS_DOI_RE, _aps_hint),
    _PublisherSpec("iop", "IOPscience", iop_key, _IOP_LIKE_RE, IOP_DOI_RE, _iop_hint),
    _PublisherSpec("tandf", "Taylor & Francis Online", tandf_key, _TANDF_LIKE_RE, TANDF_DOI_RE,
                   _tandf_hint),
    _PublisherSpec("aiaa", "AIAA", aiaa_key, _AIAA_LIKE_RE, AIAA_DOI_RE, _aiaa_hint),
    _PublisherSpec("ieee", "IEEE Xplore", ieee_key, _IEEE_LIKE_RE, IEEE_DOI_RE, _ieee_hint),
)


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


def _fetch_fulltext(cfg, args, target: str) -> int:
    """Walk the legitimate fulltext chain for ``target`` and save the file (or print the link).

    Sci-Hub is opt-in (``--scihub``, or by configuring the ``scihub`` backend) and stays last:
    it is a public copy without an open licence, so its hits say so.
    """
    key = normalize_related_key(target)
    # A pasted publisher URL, or a ``wiley:<DOI>`` / ``cambridge:<DOI>`` form, normalizes to the
    # DOI; a target shaped like one of those sites must normalize or it would drift down the
    # chain. The publisher whose own shape it is reports the error — so the site the URL lives
    # on is asked first: a generic ``/article/<DOI>`` shape (SpringerLink's) would otherwise
    # swallow another publisher's URL and leave a sub-page (``/pdf``) on the DOI.
    owned = sorted(_PUBLISHER_SPECS, key=lambda s: 0 if s.like.match(target.strip()) else 1)
    for spec in owned:
        try:
            normalized = spec.normalize(key)
        except LitcliError as e:
            if spec.like.match(target.strip()):
                note(f"[error] {e}")
                return 2
            continue
        if normalized:  # an empty answer means this publisher does not own the shape
            key = normalized[len("url:"):] if normalized.startswith("url:") else normalized[4:]
            break
    email = _resolve_oa_email(cfg, args)
    backends = get_all_backends(cfg)
    scihub_backend = backends.get("scihub")
    if args.scihub and scihub_backend is None:
        scihub_backend = _optional_backend("scihub", cfg)
    # The legal corpus sources are keyless: consult them even when not enabled in config.
    corpus = {name: backends.get(name) or _optional_backend(name, cfg)
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
            try:
                backend = _optional_backend(spec.name, cfg)
            except RuntimeError as e:  # a broken section must not sink the rest of the chain
                pub_errors[spec.name] = str(e)
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
    if info is None and unresolved:
        # A pasted page the chain could not key on: report that instead of probing every
        # backend with a URL.
        for spec, got_page in unresolved:
            page = pages[spec.name]
            if publishers.get(spec.name) is None:
                note(f"{spec.label} {page[len(spec.page_prefix):]}  "
                     f"{pub_errors.get(spec.name) or 'backend unavailable'}")
            else:
                note(f"{spec.label} {page[len(spec.page_prefix):]}  "
                     f"{'the item page gave no DOI' if got_page else 'the item page did not load'}")
                if pub_errors.get(spec.name):
                    note(f"  [skipped] {pub_errors[spec.name]}")
                else:
                    spec.hint()
        return 1
    if info is None:
        info = fulltext_mod.resolve_fulltext(key, email, backends=backends,
                                             openalex_backend=backends.get("openalex"),
                                             europepmc_backend=corpus["europepmc"],
                                             zenodo_backend=corpus["zenodo"],
                                             publishers=publishers,
                                             scihub_backend=scihub_backend)
    if not info.get("has_fulltext"):
        note(f"DOI {key}  no such work in the local store or open OA sources")
        for spec in _PUBLISHER_SPECS:
            if pub_errors.get(spec.name):
                note(f"  [skipped] {pub_errors[spec.name]}")
        for reason in info.get("errors") or []:
            note(f"  [skipped] {reason}")
        for spec in _PUBLISHER_SPECS:
            if key.lower().startswith(spec.doi_re):
                spec.hint()
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
    publisher = publishers.get(kind) or _publisher_for_url(publishers, info["url"])
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
