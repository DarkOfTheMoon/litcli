"""Wiley Online Library article reader (onlinelibrary.wiley.com), read-only.

Like ScienceDirect, Wiley answers a plain HTTP client with a Cloudflare managed challenge and
serves the article only to a session the user entitled in their browser, so the backend reads
the article page and its PDF link anyway and carries in the cookies of that session
(``[backends.wiley] .cookie``/``.cookie_file`` plus the browser's own ``.user_agent``, which
the clearance cookie is bound to).

There is no key-free search endpoint here either: a known identifier (DOI, ``wiley:<DOI>`` or
an article URL) resolves to one item. ``capabilities`` is deliberately ``{"read"}`` only (like
ScienceDirect / Sci-Hub) so the fulltext chain never mistakes a hit for a local attachment.

A configured ``tdm_token`` changes nothing about metadata, but sends the PDF download through
the TDM API (``api.wiley.com``), which bypasses Cloudflare altogether and only serves what the
user's institution subscribes to (anything else is a 403).

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

from ..errors import BadInput, BotWall, Paywalled, Unavailable
from ..models import AcademicItem, extract_year, normalize_author_name
from ..pdf import is_pdf
from ..transport import HttpError
from ._cf_gate import gate_message
from ._cf_gate import load_cookie as _load_cookie
from ._cf_gate import parse_cookie as _parse_cookie
from ._gated import GatedBackend, _clean_doi, _title_from_tag
from ._gated import _metas as _all_metas
from ._gated import doi_from as _doi_from

_SITE = "https://onlinelibrary.wiley.com"

#: A browser-ish UA: the site treats obviously scripted clients more harshly.
_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/131.0 Safari/537.36")

_BLOCK_MSG = gate_message("Wiley", "wiley")

#: Hosts the session cookies come from and the PDF may legitimately live on.
_COOKIE_HOSTS = ("onlinelibrary.wiley.com", "wiley.com")
_ALLOWED_HOSTS = ("wiley.com",)

_DOI_PREFIX_RE = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*|wiley:\s*)", re.I)


#: A DOI inside an onlinelibrary URL (``/doi/<DOI>``, ``/doi/abs/<DOI>``, ``/doi/pdf/<DOI>``…).
_URL_DOI_RE = re.compile(
    r"(?:/doi/|/doi/(?:abs|full|epdf|pdf|pdfdirect)/)(10\.\d{4,9}/[^?#\s]+)", re.I)

_LD_RE = re.compile(
    r'<script\b[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
#: A PDF anchor on the page: ``/doi/pdfdirect/<DOI>?download=true`` or ``/doi/pdf/<DOI>``.
#: Related-article cards carry theirs too, hence the captured DOI.
_PDF_HREF_RE = re.compile(
    r"""["'](/?doi/(pdfdirect|pdf|epdf)/(10\.\d{4,9}/[^"'?#\s]+)[^"']*)["']""", re.I)

#: The Wiley TDM API: an article endpoint that bypasses Cloudflare entirely. It wants a
#: ``Wiley-TDM-Client-Token`` header — any value gets an OA/entitled PDF (302 to a one-shot
#: ``alm.wiley.com`` link), a token without the entitlement gets 403.
_TDM_URL = "https://api.wiley.com/onlinelibrary/tdm/v1/articles/"


def _unescaped_when_safe(url: str) -> str:
    return html_mod.unescape((url or "").replace("\\/", "/")).strip()


def _on_wiley(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == s or host.endswith("." + s) for s in _ALLOWED_HOSTS)


# --- pure helpers (no network) ---

def doi_from(key: str) -> str:
    """Return a bare DOI from a DOI-shaped key (or the site's own prefix form), else ""."""
    return _doi_from(key, prefix_re=_DOI_PREFIX_RE)


def normalize_key(key: str) -> str:
    """Normalize a DOI, ``wiley:<DOI>``, ``doi.org`` link or article URL to ``doi:<DOI>``.

    A pasted URL may percent-encode the DOI's slash (``10.1002%2Fa%2Fb``, as the TDM API writes
    it), which the DOI shapes here do not match, so a failing key is retried decoded.
    """
    def _from(text: str) -> str:
        found = doi_from(text)
        if not found:
            m = _URL_DOI_RE.search(text)
            if m:
                found = _clean_doi(m.group(1))
        return found

    k = (key or "").strip()
    doi = _from(k)
    if not doi and "%" in k:
        doi = _from(urllib.parse.unquote(k))
    if not doi:
        raise BadInput(f"[wiley] not a DOI, wiley:<DOI> or Wiley article URL: {key!r}")
    return f"doi:{doi}"



def _to_pdfdirect(url: str) -> str:
    """Point a ``/doi/pdf/`` (or ``/doi/epdf/``) viewer shell at the real bytes.

    ``/doi/pdf/<DOI>`` answers an HTML page whose iframe loads ``/doi/pdfdirect/<DOI>``; the
    ``pdfdirect`` URL is the one that returns ``%PDF`` when the session is entitled (and a
    redirect to ``/doi/abs/`` when it is not).
    """
    return re.sub(r"/doi/(?:pdf|epdf)/(?=10\.)", "/doi/pdfdirect/", url)


def _tdm_url(doi: str) -> str:
    """The TDM endpoint for a DOI (the DOI itself percent-encoded, slashes included)."""
    return _TDM_URL + urllib.parse.quote(doi, safe="")


def _link_doi(url: str) -> str:
    m = _URL_DOI_RE.search(url or "")
    return m.group(1).lower().rstrip(".") if m else ""


def extract_pdf_url(page_html: str, doi: str = "") -> str:
    """Return the article's PDF link, or "" when the page carries none.

    A ``/doi/pdfdirect/<DOI>`` anchor wins, then the ``citation_pdf_url`` meta (always present,
    even when the session is *not* entitled — it points at the ``/doi/pdf/`` shell), then a
    plain ``/doi/pdf/<DOI>`` anchor; all are normalized to ``pdfdirect``. Related-article cards
    carry their own anchors, so when ``doi`` is given only a link naming that DOI counts. The
    result must point at a Wiley host. An empty return means "no PDF link at all".
    """
    text = page_html or ""
    want = (doi or "").lower().rstrip(".")

    def anchor(kind: str) -> str:
        for candidate, link_kind, link_doi in _PDF_HREF_RE.findall(text):
            if link_kind.lower() != kind:
                continue
            if not want or link_doi.lower().rstrip(".") == want:
                return _unescaped_when_safe(candidate)
        return ""

    raw = anchor("pdfdirect")
    if not raw:
        cite = _unescaped_when_safe((_metas(text).get("citation_pdf_url") or [""])[0])
        if cite and (not want or not _link_doi(cite) or _link_doi(cite) == want):
            raw = cite
    if not raw:
        raw = anchor("pdf") or anchor("epdf")
    if not raw:
        return ""
    if raw.startswith("//"):
        raw = "https:" + raw
    elif raw.startswith("/"):
        raw = _SITE + raw
    if not _on_wiley(raw):
        host = urlparse(raw).hostname or raw
        raise RuntimeError(f"[wiley] PDF link is not on a Wiley host: {host}")
    return _to_pdfdirect(raw)


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
    """The best scholarly JSON-LD record: one naming ``want`` if there is one, else the first.

    A page can carry a related-work record ahead of its own (``NewsArticle``/``Article`` both
    count as scholarly), so a record of another DOI must not win just by coming first.
    """
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
    return {
        "title": _text(rec.get("headline") or rec.get("name")),
        "creators": _creators_from_ld(rec),
        "abstract": _text(rec.get("description") or rec.get("abstract")),
        "doi": _record_doi(rec),
        "container": _text(part.get("name")),
        "volume": _text(part.get("volumeNumber")),
        "issue": _text(part.get("issueNumber")),
        "page": _pages(str(rec.get("pageStart") or ""), str(rec.get("pageEnd") or ""), ""),
        "publisher": _text((rec.get("publisher") or {}).get("name")
                           if isinstance(rec.get("publisher"), dict) else rec.get("publisher")),
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
        "container": _text(first("citation_journal_title", "citation_container_title")),
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

    A related-item card can ship its own JSON-LD record ahead of the article's, so a record is
    used only when it names this key; otherwise the ``citation_*`` meta is the truth and the
    record may fill just the gaps the meta leaves. The last resort is the ``<title>`` tag:
    better a title than an unparseable page.
    """
    doi = doi_from(key.split(":", 1)[1]) if key.lower().startswith("doi:") else doi_from(key)
    want = doi.lower().rstrip(".")
    metas = _metas(page_html)
    fields = _meta_fields(metas)
    ld = _ld_fields(_json_ld(page_html, want))
    matched = bool(ld["title"] or ld["creators"]) and (
        not want or (ld["doi"] or "").lower().rstrip(".") == want)
    from_ld = matched
    if matched:
        for name, value in fields.items():  # meta may still hold something the record lacks
            if value and not ld[name]:
                ld[name] = value
        fields = ld
    elif fields["title"] or fields["creators"]:
        pass  # the meta names this article; the record of another one must not overwrite it
    elif ld["title"] or ld["creators"]:
        fields, from_ld = ld, True  # no identity meta at all: the record is the only source
    else:
        fields = _dc_fields(metas)
    if not fields["title"]:
        fields["title"] = _title_from_tag(page_html, "Wiley Online Library")
    if from_ld and doi and fields["doi"] and \
            fields["doi"].lower().rstrip(".") != doi.lower().rstrip("."):
        fields["doi"] = ""  # a related card's record, not this article's: the key is the truth
    doi = fields["doi"] or doi
    return AcademicItem(
        source="wiley",
        key=key,
        title=fields["title"],
        creators=fields["creators"],
        year=fields["year"],
        abstract=fields["abstract"],
        doi=doi,
        url=f"{_SITE}/doi/{urllib.parse.quote(doi)}" if doi else "",
        container_title=fields["container"],
        volume=fields["volume"],
        issue=fields["issue"],
        page=fields["page"],
        publisher=fields["publisher"],
        item_type="article",
    )


def parse_cookie(raw: str) -> dict[str, str]:
    """Parse a cookie header / Netscape export, keeping only Wiley rows."""
    return _parse_cookie(raw, _COOKIE_HOSTS)


def load_cookie(cookie: str = "", cookie_file: str = "") -> str:
    """The cookie string from an inline string and/or a cookie file (both merged)."""
    return _load_cookie(cookie, cookie_file, _COOKIE_HOSTS, "wiley")


class WileyBackend(GatedBackend):
    """Resolve a DOI / Wiley article URL to one item and, when entitled, its PDF link.

    A session you already entitled in your own browser is carried in as a cookie string
    (``[backends.wiley] .cookie``/``.cookie_file``) together with that browser's
    ``user_agent``, and requests look like that browser's session. With a ``tdm_token`` the
    PDF goes through the TDM API instead, so no browser session is needed for the bytes.
    """

    DEFAULT_NAME = "wiley"
    SITE_NAME = "Wiley"
    KEY_PREFIX = "wiley"
    KEY_FORMS = "a DOI, wiley:<DOI> or a Wiley article URL"
    UA = _UA
    headers: ClassVar[dict] = {
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    #: Wiley's documented pace is 3 articles/s *and* 60 requests / 10 min, so ~10 s apart.
    min_interval = 10.0
    rate = 3
    COOKIE_HOSTS = _COOKIE_HOSTS
    ALLOWED_HOSTS = _ALLOWED_HOSTS
    GATE_NAME = "the Wiley clearance cookie"
    BLOCK_MSG = _BLOCK_MSG

    def __init__(self, name: str = "wiley", timeout: float = 30.0, delay: float = 10.0,
                 cookie: str = "", cookie_file: str = "", user_agent: str = "",
                 tdm_token: str = "", session=None, **_unused):
        super().__init__(name=name, timeout=timeout, delay=delay, cookie=cookie,
                         cookie_file=cookie_file, user_agent=user_agent, session=session)
        #: Optional Wiley TDM token: downloads go through ``api.wiley.com`` and skip Cloudflare.
        self.tdm_token = (tdm_token or "").strip()

    def normalize_key(self, key: str) -> str:
        """Replaces the inherited constant-driven hook: a Wiley key is always a DOI, and a
        ``wiley:`` prefix is one of the accepted forms."""
        return normalize_key(key)

    def _page(self, doi: str) -> str:
        """Fetch the article page, turning a gate into one clear error.

        Replaces the inherited ``_page`` because the page lives at ``/doi/<DOI>`` rather than
        behind a ``doi.org`` hop. The shared per-key memo is kept, so ``get`` and
        ``get_attachment`` share one request.
        """
        body = self._memo.get(doi)
        if body is not None:
            return body
        url = f"{_SITE}/doi/{urllib.parse.quote(doi)}"
        body = self._open(url).text
        self._memo[doi] = body
        return body

    # -- Backend ---------------------------------------------------------------

    def get(self, key: str) -> AcademicItem:
        """Fetch one article by DOI or URL and normalize it to ``doi:<DOI>``.

        Replaces the inherited ``get`` for the TDM route: a gated page is not fatal when a
        ``tdm_token`` is configured, because that endpoint still has the bytes.
        """
        doi = normalize_key(key)[4:]
        try:
            body = self._page(doi)
        except BotWall:
            if not self.tdm_token:  # a gate is fatal only without the token route
                raise
            # The page is walled off but the TDM API still has the bytes. An attachment must be
            # named for the chain to take the article at all, so name the TDM endpoint (a
            # browser cannot open it without the token header, hence ``extra`` keeps it apart).
            return AcademicItem(source=self.name, key=f"doi:{doi}", doi=doi,
                                url=f"{_SITE}/doi/{urllib.parse.quote(doi)}",
                                attachments=[_tdm_url(doi)],
                                extra={"tdm_url": _tdm_url(doi)}, item_type="article")
        item = page_to_item(body, f"doi:{doi}")
        # No OA marking: Wiley records no open-access signal of its own in the page.
        return self._finish_item(item, body, doi, mark_oa=False)

    def _pdf_link(self, body: str, item: AcademicItem, landed: str) -> str:
        # The key's DOI, not the page's ``citation_doi`` (a related card's may be parsed in).
        try:
            return extract_pdf_url(body, _doi_from(item.key))
        except RuntimeError:  # an odd host must not cost the metadata
            return ""

    def _tdm_pdf(self, doi: str) -> tuple[bytes | None, str]:
        """Try the TDM API first when a token is configured.

        Returns ``(bytes, "")`` on a PDF, ``(None, "")`` when the TDM route cannot say
        (400/404/5xx) and ``(None, reason)`` when it says no (401/403). The caller falls back to
        the page route either way — a stale or mistyped token must not cost a working cookie
        session — and reports the token's answer only if that route fails as well.
        """
        url = _tdm_url(doi)
        try:
            response = self.fetch(url, headers={"Wiley-TDM-Client-Token": self.tdm_token})
        except HttpError as e:
            if e.status in (401, 403):
                return None, (
                    f"[{self.name}] the TDM API says this article is not entitled "
                    f"(HTTP {e.status}): the token only buys what your institution subscribes "
                    "to — check that the token is valid and the article is covered")
            return None, ""  # 400/404/5xx: let the page route have it
        if is_pdf(response.content):
            return response.content, ""
        return None, ""

    def get_attachment(self, key: str) -> bytes:
        """Download the article PDF (an entitled session, or the TDM token, serves it)."""
        doi = normalize_key(key)[4:]
        data, tdm_reason = self._tdm_pdf(doi) if self.tdm_token else (None, "")
        if data is not None:
            return data
        try:
            page = self._page(doi)
            url = extract_pdf_url(page, doi)
        except RuntimeError:  # the page is gated too: the token's answer is the useful one
            if not tdm_reason:
                raise
            raise Unavailable(tdm_reason) from None
        if not url:
            raise Paywalled(tdm_reason or (
                f"[{self.name}] the article page carries no PDF link "
                f"(not entitled, or the session needs an off-campus login)"))
        try:
            # The item URL is where an unentitled session would have landed anyway.
            return self._gated_download(url, page, f"{_SITE}/doi/{urllib.parse.quote(doi)}")
        except Unavailable:
            # The token's answer is the more useful one when the page route is gated too.
            if not tdm_reason:
                raise
            raise Unavailable(tdm_reason) from None

    def _not_entitled_reason(self, landed: str, response) -> str:
        """Why an HTML answer means "not entitled" here (Wiley says so in a redirect header)."""
        if any(h.headers.get("atypon-access-denied") for h in response.history):
            # An unentitled session is silently redirected to the abstract page and the hop
            # carries ``atypon-access-denied: true`` (a bare HTML body could also be a viewer
            # shell, so the header is what names it).
            return "the site marked the redirect access-denied (atypon-access-denied)"
        return super()._not_entitled_reason(landed, response)
