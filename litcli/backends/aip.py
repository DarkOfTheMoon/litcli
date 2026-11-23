"""AIP Publishing reader (pubs.aip.org), read-only.

A DOI resolves through ``doi.org`` to ``pubs.aip.org`` (the legacy ``aip.scitation.org`` host
still exists for old links). The site sits behind a Cloudflare *managed challenge*: a plain
HTTP client is answered with ``403`` and ``cf-mitigated: challenge`` (the "Just a moment…"
page) instead of the article, on the page and on the ``article-pdf`` endpoint alike. The
backend therefore carries in the cookies of a session the user already cleared in their own
browser (``[backends.aip] .cookie``/``.cookie_file`` plus that browser's own ``.user_agent``,
which the ``cf_clearance`` cookie is bound to) and names the gate when they are missing.

Metadata and the PDF link come from the served page's ``citation_*`` tags; the PDF endpoint
sits on AIP's own ``/article-pdf/<id>/…`` path (the old host's ``/doi/pdf/<DOI>`` is kept as a
last resort). A body that is not ``%PDF`` is reported with what actually happened — a gate, a
missing session, or an entitlement refusal — never saved as a PDF.

``capabilities`` is deliberately ``{"read"}`` only (like ScienceDirect / Wiley / Springer /
Cambridge) so the fulltext chain never mistakes an AIP hit for a local-store attachment.

The markup is parsed by pure functions (``normalize_key``, ``page_to_item``,
``extract_pdf_url``) so a site change touches only the thin adapter, and every branch is
testable offline with a literal page snippet.
"""
from __future__ import annotations

import html as html_mod
import re
import urllib.parse
from typing import ClassVar
from urllib.parse import urlparse

from .. import provenance
from ..errors import BadInput, NotFound, Unavailable
from ..models import AcademicItem, extract_year, normalize_author_name
from ..transport import HttpError
from ._cf_gate import gate_message, is_challenge_page
from ._cf_gate import load_cookie as _load_cookie
from ._cf_gate import parse_cookie as _parse_cookie
from ._entitled import EntitledBackend

_SITE = "https://pubs.aip.org"
_LEGACY_SITE = "https://aip.scitation.org"

#: A browser-ish UA: the challenge is bound to the UA that cleared it.
_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/131.0 Safari/537.36")

_BLOCK_MSG = gate_message("AIP Publishing", "aip")

#: Hosts the session cookies come from and the PDF may legitimately live on.
_COOKIE_HOSTS = ("aip.org", "scitation.org")
_ALLOWED_HOSTS = ("aip.org", "scitation.org")

#: ``aip:`` is the documented prefix; ``aip/`` is the ``source/key`` form the table prints,
#: which a pasted key can carry — and both stack on a ``doi:`` or ``doi.org`` form.
_DOI_PREFIX_RE = re.compile(
    r"^(?:(?:https?://(?:dx\.)?doi\.org/|doi:\s*|aip[:\/]\s*)\s*)+", re.I)
_DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$")
#: Any AIP page. The modern path carries an internal numeric id, not the DOI, so a URL key
#: stays a URL until the page names its own DOI in ``citation_doi``.
_URL_RE = re.compile(r"^https?://(?:pubs\.aip\.org|aip\.scitation\.org)(?:/\S*)?$", re.I)
#: A DOI embedded in a legacy ``/doi/<DOI>``, ``/doi/abs/<DOI>``, ``/doi/pdf/<DOI>`` URL.
_URL_DOI_RE = re.compile(r"/doi/(?:abs/|full/|pdf/|epdf/)?(10\.\d{4,9}/[^?#\s]+)", re.I)
#: The article licence block: its wording (and its ``creative-commons`` class) sits on the
#: open-access pages and on neither a paywalled page nor a related-article card.
_OA_RE = re.compile(r"is an open access article|creative-commons__description", re.I)
#: The article's PDF endpoint: ``/<journal>/article-pdf/<numeric id>/<n>/<slug>`` (the journal
#: segment is optional in the match so a bare ``/article-pdf/…`` anchor still resolves).
_PDF_PATH_RE = re.compile(
    r"""((?:/[A-Za-z0-9_-]+)?/article-pdf/[A-Za-z0-9]+/[^"'?#\s]+)""", re.I)

_META_TAG_RE = re.compile(r"<meta\b[^>]*>", re.I)
_META_NAME_RE = re.compile(r"\bname=[\"']([^\"']+)[\"']", re.I)
_META_CONTENT_RE = re.compile(r"\bcontent=[\"']([^\"']*)[\"']", re.I)
_TITLE_TAG_RE = re.compile(r"<title\b[^>]*>(.*?)</title>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _unescaped_when_safe(url: str) -> str:
    return html_mod.unescape((url or "").replace("\\/", "/")).strip()


def _on_aip(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == s or host.endswith("." + s) for s in _ALLOWED_HOSTS)


# --- pure helpers (no network) ---

def doi_from(key: str) -> str:
    """Return a bare DOI from a DOI-shaped key (a ``doi.org``/``aip:`` form included)."""
    k = _DOI_PREFIX_RE.sub("", (key or "").strip()).strip()
    return k if _DOI_RE.match(k) else ""


def normalize_key(key: str) -> str:
    """Classify a key and return ``doi:<DOI>`` or ``url:<AIP URL>``.

    A pasted URL may percent-encode the DOI, and a legacy URL carries it in a ``/doi/`` path,
    so a failing key is retried decoded and searched for an embedded DOI.
    """
    k = (key or "").strip()
    if k.lower().startswith("url:"):  # an already-normalized key keeps its kind
        k = k[len("url:"):].strip()
    k = re.sub(r"^(?:aip[:/]\s*)+", "", k, flags=re.I).strip()
    if not k:
        raise BadInput("[aip] empty key (expected a DOI, aip:<DOI> or AIP article URL)")
    for candidate in ((k, urllib.parse.unquote(k)) if "%" in k else (k,)):
        if _URL_RE.match(candidate):
            embedded = _URL_DOI_RE.search(candidate)
            if embedded:  # a legacy /doi/<DOI> URL: the DOI is the better key
                return f"doi:{embedded.group(1).rstrip('.')}"
            return f"url:{candidate.split('#', 1)[0]}"
        doi = doi_from(candidate)
        if doi:
            return f"doi:{doi.rstrip('.')}"
    raise BadInput(f"[aip] not a DOI, aip:<DOI> or AIP article URL: {key!r}")


def is_block_page(page_html: str, status: int = 200) -> bool:
    """True when the body is the Cloudflare challenge page, or the status is a plain 403."""
    return is_challenge_page(page_html, status)


def is_open_access(page_html: str) -> bool:
    """True when the page carries an article licence (its PDF needs no session)."""
    return bool(_OA_RE.search(page_html or ""))


def item_id(url: str) -> str:
    """The internal article id out of an article URL (the numeric segment before the slug)."""
    # The id is the last numeric segment before the slug (greedy: volume/issue/page precede it).
    match = re.search(r"/article/.*?/(\d+)/[^/]+/?$", urlparse(url).path or "")
    return match.group(1) if match else ""


def extract_pdf_url(page_html: str, doi: str = "", want_id: str = "") -> str:
    """Return the article's PDF link, or "" when the page carries none.

    The page's own ``citation_pdf_url`` meta is authoritative; older/alternate markup carries a
    plain ``/article-pdf/…`` anchor instead, and related-article cards carry the same shape — so
    an anchor only counts when its path names this article's id (``want_id``). ``doi`` is a
    last-resort rewrite (the old ``/doi/pdf/<DOI>`` URL). The result must point at AIP — a third
    party here means the page handed back something unexpected; raising keeps that visible.
    """
    text = page_html or ""
    raw = _unescaped_when_safe((_metas(text).get("citation_pdf_url") or [""])[0])
    if not raw:
        first = ""
        for candidate in _PDF_PATH_RE.findall(text):
            first = first or candidate
            if want_id and f"/{want_id}/" in candidate:
                raw = candidate
                break
        raw = _unescaped_when_safe(raw or first)
    if not raw and doi:
        raw = f"{_SITE}/doi/pdf/{urllib.parse.quote(doi, safe='/')}"
    if not raw:
        return ""
    if raw.startswith("//"):
        raw = "https:" + raw
    elif raw.startswith("/"):
        raw = _SITE + raw
    if not _on_aip(raw):
        host = urlparse(raw).hostname or raw
        raise RuntimeError(f"[aip] PDF link is not on an AIP host: {host}")
    return raw


def _text(value) -> str:
    """Strip tags/entities and collapse whitespace."""
    return _WS_RE.sub(" ", html_mod.unescape(_TAG_RE.sub(" ", str(value or "")))).strip()


def _metas(page_html: str) -> dict[str, list[str]]:
    """Collect ``citation_*`` / ``dc.*`` meta values, keyed by lowercased name."""
    found: dict[str, list[str]] = {}
    for tag in _META_TAG_RE.findall(page_html or ""):
        name = _META_NAME_RE.search(tag)
        content = _META_CONTENT_RE.search(tag)
        if not name or not content:
            continue
        key = name.group(1).lower()
        if key.startswith(("citation_", "dc.")):
            found.setdefault(key, []).append(content.group(1))
    return found


def _pages(first: str, last: str, single: str) -> str:
    if first and last and first != last:
        return f"{first}-{last}"
    return single or first


def _fields(metas: dict[str, list[str]]) -> dict:
    """Read the item fields out of ``citation_*`` meta, falling back to ``dc.*``."""
    def first(*names: str) -> str:
        for name in names:
            values = metas.get(name) or []
            if values:
                return values[0]
        return ""

    return {
        "title": _text(first("citation_title", "dc.title")),
        "creators": [a for a in (metas.get("citation_author")
                                 or metas.get("dc.creator") or []) if a.strip()],
        "container": _text(first("citation_journal_title", "citation_conference_title",
                                 "dc.source")),
        "volume": first("citation_volume"),
        "issue": first("citation_issue"),
        "page": _pages(first("citation_firstpage"), first("citation_lastpage"),
                       first("citation_pages")),
        "year": extract_year(first("citation_publication_date", "citation_date", "dc.date")),
        "doi": doi_from(first("citation_doi", "dc.identifier")),
        "abstract": _text(first("citation_abstract", "dc.description")),
        "publisher": _text(first("citation_publisher", "dc.publisher")),
    }


def page_to_item(page_html: str, key: str, url: str = "") -> AcademicItem:
    """Normalize an article page to an :class:`AcademicItem` (``citation_*`` → ``dc.*`` → title).

    ``key`` is the normalized key; the page's own ``citation_doi`` is the truth for ``doi``
    (a ``url:`` key names only an internal numeric id).
    """
    page = page_html or ""
    fields = _fields(_metas(page))
    if not fields["title"]:
        title_tag = _TITLE_TAG_RE.search(page)
        if title_tag:
            title = _text(title_tag.group(1))
            fields["title"] = re.sub(r"\s*[|—-]\s*AIP Publishing\s*$", "", title).strip()
    doi = fields["doi"] or doi_from(key.split(":", 1)[-1])
    creators = [normalize_author_name(a) for a in fields["creators"]]
    return AcademicItem(
        source="aip",
        key=key,
        title=fields["title"],
        creators=[c for c in creators if c["family"] or c["given"]],
        year=fields["year"],
        abstract=fields["abstract"],
        doi=doi,
        url=url or (key[len("url:"):] if key.startswith("url:") else ""),
        container_title=fields["container"],
        volume=fields["volume"],
        issue=fields["issue"],
        page=fields["page"],
        publisher=fields["publisher"],
        item_type="article",
    )


def parse_cookie(raw: str) -> dict[str, str]:
    """Parse a cookie header / Netscape export, keeping only AIP rows."""
    return _parse_cookie(raw, _COOKIE_HOSTS)


def load_cookie(cookie: str = "", cookie_file: str = "") -> str:
    """The ``Cookie`` header value from an inline string and/or a cookie file (both merged)."""
    return _load_cookie(cookie, cookie_file, _COOKIE_HOSTS, "aip")


class AipBackend(EntitledBackend):
    """Resolve a DOI / AIP article URL to one item and, when entitled, its PDF link.

    The site is behind a Cloudflare managed challenge, so a session you already cleared in
    your own browser is carried in as a ``Cookie`` header (``[backends.aip]
    .cookie``/``.cookie_file``) together with that browser's ``user_agent``, and requests look
    like that browser's session. Without one, both the page and the PDF answer a gate.
    """

    UA = _UA
    headers: ClassVar[dict] = {
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    min_interval = 1.0
    rate = 4
    COOKIE_HOSTS = _COOKIE_HOSTS
    GATE_NAME = "the AIP clearance cookie"
    BLOCK_MSG = _BLOCK_MSG

    def __init__(self, name: str = "aip", timeout: float = 30.0, delay: float = 1.0,
                 cookie: str = "", cookie_file: str = "", user_agent: str = "", **_unused):
        super().__init__(name=name, timeout=timeout, delay=delay, cookie=cookie,
                         cookie_file=cookie_file, user_agent=user_agent)
        self._last_key = self._last_alias = self._last_page = self._last_url = ""

    def _page(self, key: str) -> tuple[str, str]:
        """Fetch the article page as ``(html, landed url)``, turning a gate into one clear error.

        The page is memoized per key: the PDF download and the item read share one request. A
        DOI goes through ``doi.org`` (it redirects to ``pubs.aip.org``; the legacy
        ``aip.scitation.org`` host is still answered for old links).
        """
        normalized = normalize_key(key)
        if self._last_page and normalized in (self._last_key, self._last_alias):
            return self._last_page, self._last_url
        if normalized.startswith("url:"):
            url = normalized[len("url:"):]
        else:
            url = f"https://doi.org/{urllib.parse.quote(normalized[len('doi:'):], safe='/')}"
        response = self._open(url)  # a gate is turned into BLOCK_MSG; 404 propagates
        landed = str(response.url) or url
        if not _on_aip(landed):
            raise NotFound(f"[{self.name}] DOI does not resolve to an AIP article: {normalized}")
        self._last_key, self._last_alias = normalized, ""
        self._last_page, self._last_url = response.text, landed
        return self._last_page, landed

    # -- Backend ---------------------------------------------------------------

    def search(self, query: str, limit: int = 10, filters: dict | None = None) -> list[AcademicItem]:
        """Resolve a DOI / URL query to one item; keyword queries return [].

        There is no key-free search endpoint (the site search needs the same cleared session
        and is JavaScript-driven), so anything that is not already an identifier cannot be
        looked up here. ``filters`` are not applied: there is at most one hit.
        """
        try:
            normalize_key(query or "")
        except BadInput:
            return []
        if limit < 1:
            return []
        try:
            return [self.get(query)]
        except (NotFound, KeyError):
            return []

    def get(self, key: str) -> AcademicItem:
        """Fetch one article by DOI or URL and, when the page names one, its PDF link.

        A 404 is turned into :class:`NotFound` so a search treating the key as a candidate
        yields nothing instead of an error.
        """
        normalized = normalize_key(key)
        try:
            body, landed = self._page(normalized)
        except HttpError as e:
            if e.status in (404, 410):
                raise NotFound(f"[{self.name}] no such AIP article: {key}") from e
            raise
        item = page_to_item(body, normalized, landed)
        if normalized.startswith("url:") and item.doi:
            # The URL carried only an internal id; the page named the real identity, which also
            # becomes the memo key so the PDF download reuses this page.
            item.key = f"doi:{item.doi}"
            self._last_alias = f"doi:{item.doi}"
        if not item.title:
            raise RuntimeError(f"[{self.name}] could not parse the article page: {landed}")
        try:
            pdf_url = extract_pdf_url(body, item.doi, item_id(landed))
        except RuntimeError:  # an odd host must not cost the metadata
            pdf_url = ""
        if pdf_url:
            item.attachments = [pdf_url]
        if is_open_access(body):
            # The chain labels the hit from this: an open-access article is not a subscription
            # copy, so its note and its "needs a session" advice would both be wrong.
            item.extra = {**item.extra, "open_access": True}
            item.access = provenance.OA
        return item

    def get_attachment(self, key: str) -> bytes:
        """Download the article PDF (only a session the site lets through serves it)."""
        normalized = normalize_key(key)
        page, landed = self._page(normalized)
        url = extract_pdf_url(page, doi_from(normalized), item_id(landed))
        if not url:
            raise Unavailable(f"[{self.name}] the article page carries no PDF link: {landed}")
        try:
            response = self.fetch(url, headers={"Accept": "application/pdf"})
        except HttpError as e:
            if is_block_page(e.body, e.status):
                raise Unavailable(f"[{self.name}] PDF download was gated: {_BLOCK_MSG}") from e
            raise
        if response.content.startswith(b"%PDF"):
            return response.content
        # An unentitled session is sent back to the article page (HTML, never a 403).
        landed_on = urlparse(str(response.url)).path or url
        if is_open_access(page):
            why = "the article is open access but the endpoint answered an HTML page"
        elif not self.has_session:
            why = "the session holds no cleared Cloudflare cookie"
        else:
            why = "the session is not entitled to this article (expired, or not subscribed)"
        raise Unavailable(
            f"[{self.name}] no PDF bytes: {why} (landed on {landed_on}) — open {url} in your "
            "entitled browser, or refresh the cookie+user_agent pair from a cleared tab")
