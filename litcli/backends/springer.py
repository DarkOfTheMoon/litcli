"""SpringerLink reader (link.springer.com), read-only.

Items are journal articles, book chapters and reference-work entries; each answers only on its
own path (``/article/``, ``/chapter/``, ``/referenceworkentry/``), so the path is probed.

Unlike ScienceDirect and Wiley, SpringerLink serves the item page to a plain HTTP client
with no Cloudflare gate and carries the full Highwire ``citation_*`` tags even anonymously, so
metadata always works. What still needs a session is the PDF: the endpoint answers any client
that does not hold a Springer identity cookie with a 303 to ``idp.springer.com``, and a session
that is neither entitled nor looking at an open-access article simply lands back on the article
page (open-access PDFs come straight back as ``%PDF``).

The backend therefore carries in the cookies of a session the user entitled in their browser
(``[backends.springer] .cookie``/``.cookie_file`` plus that browser's own ``.user_agent``).
The identity cookie is bound to the UA that earned it, so both travel together.

``capabilities`` is deliberately ``{"read"}`` only (like ScienceDirect / Wiley) so the fulltext
chain never mistakes a Springer hit for a local-store attachment.

The markup is parsed by pure functions (``normalize_key``, ``page_to_item``,
``extract_pdf_url``) so a site change touches only the thin adapter, and every branch is
testable offline with a literal page snippet.
"""
from __future__ import annotations

import html as html_mod
import json
import re
import urllib.parse
from typing import ClassVar
from urllib.parse import urlparse

from ..errors import BadInput, NotFound, Paywalled
from ..models import AcademicItem, extract_year, normalize_author_name
from ..transport import HttpError
from ._cf_gate import load_cookie as _load_cookie
from ._cf_gate import parse_cookie as _parse_cookie
from ._gated import _DOI_RE, GatedBackend, _clean_doi, _title_from_tag
from ._gated import _metas as _all_metas
from ._gated import doi_from as _doi_from

_SITE = "https://link.springer.com"

#: A browser-ish UA: the site treats obviously scripted clients more harshly.
_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/131.0 Safari/537.36")

#: SpringerLink has no Cloudflare, so ``gate_message``'s challenge wording would be wrong here.
_BLOCK_MSG = (
    "SpringerLink served its bot gate instead of the item: open the article once in an "
    "authenticated browser tab on your off-campus session, then copy that session's cookies "
    "into [backends.springer] cookie / cookie_file — with the browser's own user_agent, and "
    "from the same machine")

#: Hosts the session cookies come from (the identity cookie lives on ``springer.com``; a
#: Nature- or BMC-branded article keeps its own session on its own site) and the PDF may
#: legitimately live on (those brands serve it from ``nature.com`` / ``biomedcentral.com``).
_COOKIE_HOSTS = ("link.springer.com", "www.nature.com", "springer.com", "nature.com",
                 "biomedcentral.com")
#: ``springernature.com`` stays out of ``_COOKIE_HOSTS``: a browser marks ``cf_clearance``
#: host-only too, so it gets no cookie there. Add it to both lists if it ever checks a session.
_ALLOWED_HOSTS = ("springer.com", "springernature.com", "nature.com", "biomedcentral.com")

#: ``springer:`` is the documented prefix; ``springer/`` is the ``source/key`` form the table
#: prints, which a pasted key can carry.
#: Repeatable: a pasted key may carry ``springer/`` *and* ``doi:`` (the ``source/key`` form the
#: table prints prepends to a key that already says ``doi:``).
_DOI_PREFIX_RE = re.compile(r"^(?:(?:https?://(?:dx\.)?doi\.org/|doi:\s*|springer[:\/]\s*)\s*)+", re.I)


#: A page shape a DOI lives under. A journal article answers only on ``/article/``, a book
#: chapter only on ``/chapter/`` and a reference-work entry only on ``/referenceworkentry/``;
#: the other two 404. All three carry the same ``citation_*`` tags and ``citation_pdf_url``, so
#: the path is probed rather than derived from the DOI.
_PATH_KINDS = ("article", "chapter", "referenceworkentry")

#: A DOI inside a SpringerLink (``/article/``, ``/chapter/``, ``/content/pdf/…``,
#: ``/referenceworkentry/``) or BMC (``/articles/``) URL. Nature-branded content lives on
#: nature.com under a *slug*, not a DOI — that is ``_NATURE_PDF_RE``'s job.
_URL_DOI_RE = re.compile(
    r"/(?:articles?|chapter|referenceworkentry)/(10\.\d{4,9}/[^?#\s]+)"
    r"|/content/pdf/(10\.\d{4,9}/[^?#\s]+)\.pdf", re.I)
#: A sub-page hung off a DOI (``/figures/1``, ``/sections/…``, IOP's ``/pdf`` and ``/meta``):
#: the DOI ends before it.
_SUBPAGE_RE = re.compile(
    r"/(?:figures|tables|media|metrics|statistics|sections|supplementary-material|"
    r"supplementary-information|pdf|epdf|abs|meta|full|references|citedby)(?:/[^?#]*)?$", re.I)

_LD_RE = re.compile(
    r'<script\b[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
#: A PDF anchor on the page (``href="/content/pdf/<DOI>.pdf"``). Related-article cards carry
#: theirs too, hence the captured DOI.
_PDF_HREF_RE = re.compile(
    r"""["'](/?content/pdf/(10\.\d{4,9}/[^"'?#\s]+)\.pdf)[^"']*["']""", re.I)
#: The badge an open-access article page carries (and a paywalled one does not).
_OA_MARKER = 'data-test="open-access"'


def _unescaped_when_safe(url: str) -> str:
    return html_mod.unescape((url or "").replace("\\/", "/")).strip()


def _on_springer(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == s or host.endswith("." + s) for s in _ALLOWED_HOSTS)


# --- pure helpers (no network) ---

def doi_from(key: str) -> str:
    """Return a bare DOI from a DOI-shaped key (or the site's own prefix form), else ""."""
    return _doi_from(key, prefix_re=_DOI_PREFIX_RE)


def _trim_subpage(doi: str) -> str:
    """Drop a sub-page tail (``/figures/1``) from a captured DOI, when a DOI still remains."""
    trimmed = _SUBPAGE_RE.sub("", doi).rstrip("/")
    return trimmed if _DOI_RE.match(trimmed) else doi


def normalize_key(key: str) -> str:
    """Normalize a DOI, ``springer:<DOI>``, ``doi.org`` link or article URL to ``doi:<DOI>``.

    Nature-branded URLs carry a slug rather than a DOI (``nature.com/articles/<slug>``, the DOI
    being ``10.1038/<slug>``); a pasted URL may percent-encode the DOI's slash, which the DOI
    shapes here do not match, so a failing key is retried decoded.
    """
    def _from(text: str) -> str:
        # The URL shape first: a ``/content/pdf/<DOI>.pdf`` link ends in a suffix the bare DOI
        # finder would keep (the URL regex knows to drop it).
        m = _URL_DOI_RE.search(text)
        if m:
            return _trim_subpage(_clean_doi(m.group(1) or m.group(2)))
        found = doi_from(text)
        if found:
            return _trim_subpage(found)
        nature = _NATURE_PDF_RE.search(text.split("?")[0].split("#")[0])
        return f"10.1038/{nature.group(1)}" if nature else ""

    k = (key or "").strip()
    doi = _from(k)
    if not doi and "%" in k:
        doi = _from(urllib.parse.unquote(k))
    if not doi:
        raise BadInput(f"[springer] not a DOI, springer:<DOI> or SpringerLink article URL: {key!r}")
    return f"doi:{doi}"



def is_open_access(page_html: str) -> bool:
    """True when the page badges the article as open access (its PDF needs no session)."""
    return _OA_MARKER in (page_html or "")


#: A Nature-branded PDF: ``https://www.nature.com/articles/<suffix>.pdf`` (the DOI is
#: ``10.1038/<suffix>``).
_NATURE_PDF_RE = re.compile(r"nature\.com/articles/([a-z0-9.-]+?)(?:\.epdf|\.pdf)?$", re.I)


def _link_doi(url: str) -> str:
    """The DOI a PDF link names, or "" when the link's shape carries none."""
    m = _URL_DOI_RE.search(url or "")
    if m:
        return (m.group(1) or m.group(2) or "").lower().rstrip(".")
    nature = _NATURE_PDF_RE.search((url or "").split("?")[0])
    return f"10.1038/{nature.group(1)}".lower() if nature else ""


def extract_pdf_url(page_html: str, doi: str = "") -> str:
    """Return the article's PDF link, or "" when the page carries none.

    ``citation_pdf_url`` is present on every SpringerLink article page, entitled or not (it is
    the ``/content/pdf/<DOI>.pdf`` endpoint; Nature-branded content points at ``nature.com``),
    so it is the normal source; a plain ``/content/pdf/`` anchor is the fallback. Related-article
    cards carry their own anchors, so when ``doi`` is given only a link naming that DOI counts.
    The result must point at a Springer Nature host. An empty return means "no PDF link at all".
    """
    text = page_html or ""
    want = (doi or "").lower().rstrip(".")

    cite = _unescaped_when_safe((_metas(text).get("citation_pdf_url") or [""])[0])
    raw = ""
    if cite and (not want or not _link_doi(cite) or _link_doi(cite) == want):
        raw = cite
    if not raw:
        for candidate, link_doi in _PDF_HREF_RE.findall(text):
            if not want or link_doi.lower().rstrip(".") == want:
                raw = _unescaped_when_safe(candidate)
                break
    if not raw:
        return ""
    if raw.startswith("//"):
        raw = "https:" + raw
    elif raw.startswith("/"):
        raw = _SITE + raw
    if not _on_springer(raw):
        host = urlparse(raw).hostname or raw
        raise RuntimeError(f"[springer] PDF link is not on a Springer Nature host: {host}")
    return raw


def _text(value) -> str:
    """Strip tags/entities and collapse whitespace."""
    return _WS_RE.sub(" ", html_mod.unescape(_TAG_RE.sub(" ", str(value or "")))).strip()


def _as_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _metas(page_html: str) -> dict[str, list[str]]:
    """Collect every ``<meta name=… content=…>`` pair (this parser reads non-item names too)."""
    return _all_metas(page_html, keep=None)


def _record_doi(rec: dict) -> str:
    """The bare DOI a JSON-LD record names, or ""."""
    for cand in _as_list(rec.get("identifier")):
        if isinstance(cand, dict):
            cand = cand.get("value")
        doi = doi_from(str(cand or ""))
        if doi:
            return doi
    return ""


def _json_ld(page_html: str, want: str = ""):
    """The best scholarly JSON-LD record: one naming ``want`` if there is one, else the first."""
    fallback = {}
    for blob in _LD_RE.findall(page_html or ""):
        try:
            data = json.loads(blob)
        except (ValueError, TypeError):
            continue
        for rec in _as_list(data):
            if not isinstance(rec, dict):
                continue
            kinds = " ".join(str(t) for t in _as_list(rec.get("@type"))).lower()
            if not ("article" in kinds or "chapter" in kinds):
                continue
            if want and _record_doi(rec).lower().rstrip(".") == want:
                return rec
            fallback = fallback or rec
    return fallback


def _pages(first: str, last: str, single: str) -> str:
    first, last, single = first.strip(), last.strip(), single.strip()
    if first and last and last != first:
        return f"{first}-{last}"
    return first or single


def _creators_from_ld(rec: dict) -> list[dict[str, str]]:
    creators = []
    for author in _as_list(rec.get("author")):
        if isinstance(author, dict):
            name = _text(author.get("name"))
            if name:
                creators.append(normalize_author_name(name))
        elif isinstance(author, str) and author.strip():
            creators.append(normalize_author_name(author))
    return creators


def _ld_fields(rec: dict) -> dict:
    part = rec.get("isPartOf") if isinstance(rec.get("isPartOf"), dict) else {}
    publisher = rec.get("publisher")
    return {
        "title": _text(rec.get("headline") or rec.get("name")),
        "creators": _creators_from_ld(rec),
        "abstract": _text(rec.get("description") or rec.get("abstract")),
        "doi": _record_doi(rec),
        "container": _text(part.get("name")),
        "volume": _text(part.get("volumeNumber")),
        "issue": _text(part.get("issueNumber")),
        "page": _pages(str(rec.get("pageStart") or ""), str(rec.get("pageEnd") or ""), ""),
        "publisher": _text(publisher.get("name") if isinstance(publisher, dict) else publisher),
        "year": extract_year(str(rec.get("datePublished") or "")),
    }


def _meta_fields(metas: dict[str, list[str]]) -> dict:
    def first(*names: str) -> str:
        for name in names:
            values = metas.get(name) or []
            if values and values[0].strip():
                return values[0]
        return ""

    return {
        "title": _text(first("citation_title")),
        "creators": [normalize_author_name(a) for a in metas.get("citation_author") or []
                     if a.strip()],
        "abstract": _text(first("citation_abstract")),
        "doi": doi_from(first("citation_doi")),
        "container": _text(first("citation_journal_title", "citation_container_title",
                                 "citation_series_title")),
        "volume": _text(first("citation_volume")),
        "issue": _text(first("citation_issue")),
        "page": _pages(first("citation_firstpage"), first("citation_lastpage"),
                       first("citation_pages", "citation_page")),
        "publisher": _text(first("citation_publisher")),
        "year": extract_year(first("citation_publication_date", "citation_date")),
    }


def _dc_fields(metas: dict[str, list[str]]) -> dict:
    def first(*names: str) -> str:
        for name in names:
            values = metas.get(name) or []
            if values and values[0].strip():
                return values[0]
        return ""

    return {
        "title": _text(first("dc.title")),
        "creators": [normalize_author_name(a) for a in metas.get("dc.creator") or []
                     if a.strip()],
        "abstract": _text(first("dc.description")),
        "doi": doi_from(first("dc.identifier")),
        "container": _text(first("dc.source")),
        "volume": "",
        "issue": "",
        "page": "",
        "publisher": _text(first("dc.publisher")),
        "year": extract_year(first("dc.date")),
    }


def page_to_item(page_html: str, key: str) -> AcademicItem:
    """Normalize an article page to an :class:`AcademicItem` (citation_* → JSON-LD → dc.*).

    The last resort is the ``<title>`` tag: better a title than an unparseable page.
    """
    doi = doi_from(key.split(":", 1)[1]) if key.lower().startswith("doi:") else doi_from(key)
    metas = _metas(page_html)
    fields = _meta_fields(metas)
    from_ld = False
    if not fields["title"] or not fields["creators"]:
        # Fill the gaps only: a page can carry a title but no authors, or the reverse, and the
        # JSON-LD record may hold exactly what the meta tags lack.
        ld = _ld_fields(_json_ld(page_html, doi.lower().rstrip(".")))
        from_ld = bool(ld["title"] or ld["creators"])
        for name, value in ld.items():
            if value and not fields[name]:
                fields[name] = value
    if not fields["title"] and not fields["creators"]:
        fields = _dc_fields(metas)
    if not fields["title"]:
        fields["title"] = _title_from_tag(page_html, "SpringerLink")
    if from_ld and doi and fields["doi"] and \
            fields["doi"].lower().rstrip(".") != doi.lower().rstrip("."):
        fields["doi"] = ""  # a related card's record, not this article's: the key is the truth
    doi = fields["doi"] or doi
    return AcademicItem(
        source="springer",
        key=key,
        title=fields["title"],
        creators=fields["creators"],
        year=fields["year"],
        abstract=fields["abstract"],
        doi=doi,
        url=f"{_SITE}/article/{urllib.parse.quote(doi, safe='/')}" if doi else "",
        container_title=fields["container"],
        volume=fields["volume"],
        issue=fields["issue"],
        page=fields["page"],
        publisher=fields["publisher"],
        item_type="article",
    )


def parse_cookie(raw: str) -> dict[str, str]:
    """Parse a cookie header / Netscape export, keeping only Springer rows."""
    return _parse_cookie(raw, _COOKIE_HOSTS)


def load_cookie(cookie: str = "", cookie_file: str = "") -> str:
    """The cookie string from an inline string and/or a cookie file (both merged)."""
    return _load_cookie(cookie, cookie_file, _COOKIE_HOSTS, "springer")


class SpringerBackend(GatedBackend):
    """Resolve a DOI / SpringerLink article URL to one item and its PDF link.

    A session you already entitled in your own browser is carried in as a cookie string
    (``[backends.springer] .cookie``/``.cookie_file``) together with that browser's
    ``user_agent``. The page itself needs no session (metadata works anonymously); the PDF
    download does, and only for articles your institution subscribes to — open-access ones
    download without any session at all.
    """

    DEFAULT_NAME = "springer"
    SITE_NAME = "SpringerLink"
    KEY_PREFIX = "springer"
    KEY_FORMS = "a DOI, springer:<DOI> or a SpringerLink article URL"
    UA = _UA
    headers: ClassVar[dict] = {
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    min_interval = 1.0
    rate = 4
    COOKIE_HOSTS = _COOKIE_HOSTS
    ALLOWED_HOSTS = _ALLOWED_HOSTS
    GATE_NAME = "the SpringerLink identity cookie"
    #: Its page (and PDF link) answers anonymously; only the bytes want a session.
    ANONYMOUS_PAGES = True
    BLOCK_MSG = _BLOCK_MSG

    def __init__(self, name: str = "springer", timeout: float = 30.0, delay: float = 1.0,
                 cookie: str = "", cookie_file: str = "", user_agent: str = "",
                 session=None, **_unused):
        super().__init__(name=name, timeout=timeout, delay=delay, cookie=cookie,
                         cookie_file=cookie_file, user_agent=user_agent, session=session)

    def normalize_key(self, key: str) -> str:
        """Replaces the inherited constant-driven hook: a Nature URL carries a slug rather than
        a DOI, and a ``springer:`` prefix is one of the accepted forms."""
        return normalize_key(key)

    def is_open_access(self, page_html: str) -> bool:
        """SpringerLink badges the article instead of carrying a licence block."""
        return is_open_access(page_html)

    def _page(self, doi: str) -> tuple[str, str]:
        """Fetch the item page as ``(html, url)``, turning a gate into one clear error.

        Replaces the inherited ``_page`` because the DOI's page kind is not derivable from the
        DOI: the kinds are probed in turn and a 404 simply moves on (a chapter and a
        reference-work entry each answer only on their own path). The shared per-key memo is
        kept, so the PDF download and the item read share one request. The first hit 303s to
        ``idp.springer.com``, which sets the identity cookie and sends the client back; the
        shared cookie jar carries it, so the hop is transparent.
        """
        hit = self._memo.get(doi)
        if hit is not None:
            return hit
        quoted = urllib.parse.quote(doi, safe="/")
        missing: HttpError | None = None
        for kind in _PATH_KINDS:
            url = f"{_SITE}/{kind}/{quoted}"
            try:
                # A gate becomes the backend's own message; a 5xx still retries.
                body = self._open(url).text
            except HttpError as e:
                if e.status == 404:
                    missing = missing or e
                    continue
                raise
            self._memo[doi] = (body, url)
            return body, url
        if missing is not None:  # every kind 404ed: the caller turns this into NotFound
            raise missing
        raise RuntimeError(f"[{self.name}] no page kind tried for doi:{doi}")

    # -- Backend ---------------------------------------------------------------

    def get(self, key: str) -> AcademicItem:
        """Fetch one article by DOI or URL and normalize it to ``doi:<DOI>``.

        Replaces the inherited ``get`` because the landed URL decides the item type (a chapter
        or reference-work entry is not a journal article). A 404 is turned into
        :class:`NotFound` so a search treating the key as a candidate yields nothing.
        """
        doi = normalize_key(key)[4:]
        try:
            body, url = self._page(doi)
        except HttpError as e:
            if e.status == 404:
                raise NotFound(f"[{self.name}] no such SpringerLink item: doi:{doi}") from e
            raise
        item = page_to_item(body, f"doi:{doi}")
        item.url = url  # a chapter answers on /chapter/, not the /article/ default
        if "/chapter/" in url or "/referenceworkentry/" in url:
            item.item_type = "chapter"  # a book chapter / encyclopedia entry, not a journal article
        # No ``_short_page`` to guard: an unentitled session is sent back to the same article
        # page, so there is no related-item-card short page to mistake the licence block for.
        return self._finish_item(item, body, url)

    def _pdf_link(self, body: str, item: AcademicItem, landed: str) -> str:
        # The key's DOI, not the page's ``citation_doi`` (a related card's may be parsed in).
        try:
            return extract_pdf_url(body, _doi_from(item.key))
        except RuntimeError:  # an odd host must not cost the metadata
            return ""

    def _entitled(self) -> bool:
        """Whether the session holds a filled-in Springer institution token.

        ``sim-inst-token`` is set to the empty string for an anonymous visitor and to the
        entitlement token once the session is recognised. The PDF landing check uses it to
        choose which failure reason to report; it never decides whether to attempt a download.

        The configured cookie is checked first: it is sent as a header and never enters the
        shared jar, so reading the jar alone would report a correctly configured session as
        having no token at all.
        """
        configured = parse_cookie(self.cookie).get("sim-inst-token") or ""
        if configured.strip().strip('"').strip():
            return True
        try:
            # The site quotes the value: anonymous visitors get ``""``, not a missing cookie.
            value = str(self.http.cookies.get("sim-inst-token") or "").strip().strip('"').strip()
        except Exception:  # a jar without the cookie is simply "not entitled"
            return False
        return bool(value)

    def get_attachment(self, key: str) -> bytes:
        """Download the article PDF (an entitled session, or open access, serves it)."""
        doi = normalize_key(key)[4:]
        page, landed = self._page(doi)
        url = extract_pdf_url(page, doi)
        if not url:
            raise Paywalled(
                f"[{self.name}] the article page carries no PDF link for doi:{doi}")
        return self._gated_download(url, page, landed)

    def _refusal_reason(self, page: str, landed: str, response) -> str:
        """Why the PDF endpoint answered HTML: the institution token is Springer's own state."""
        if self.is_open_access(page):
            return "the article is open access but the endpoint answered an HTML page"
        if self._entitled():
            return ("the session's institution token was not accepted (expired, or not "
                    "subscribed to this title)")
        return ("the session holds no institution token (sim-inst-token), so the download "
                "landed on the article page")
