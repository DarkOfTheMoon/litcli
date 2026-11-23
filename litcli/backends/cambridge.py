"""Cambridge Core reader (cambridge.org), read-only.

Items are journal articles and book chapters. A DOI resolves through ``doi.org`` to the item's
page; a paywalled item lands on its ``/abs/`` short page, while an open-access one (or one the
session is entitled to) opens the full page. Unlike the Cloudflare-walled readers, Cambridge
serves that page to a plain HTTP client with no challenge at all and carries the full Highwire
``citation_*`` tags anonymously, so metadata always works.

What needs more than metadata is the PDF: the ``content/view`` endpoint answers an anonymous
client with a 302 back to the item page (an HTML body, never a 403), and only an open-access
article's PDF comes straight back as ``%PDF``. The backend therefore carries in the cookies of a
session the user entitled in their browser (``[backends.cambridge] .cookie``/``.cookie_file``
plus that browser's own ``.user_agent``), and says what the endpoint answered when the bytes do
not come (no link, a gate, or an entitlement refusal).

``capabilities`` is deliberately ``{"read"}`` only (like ScienceDirect / Wiley / Springer) so
the fulltext chain never mistakes a Cambridge hit for a local-store attachment.

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
from ._cf_gate import is_challenge_page
from ._cf_gate import load_cookie as _load_cookie
from ._cf_gate import parse_cookie as _parse_cookie
from ._entitled import EntitledBackend

_SITE = "https://www.cambridge.org"

#: A browser-ish UA: the site treats obviously scripted clients more harshly.
_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/131.0 Safari/537.36")

#: Cambridge is not Cloudflare-walled today (it only sets a ``__cf_bm`` bot-management cookie),
#: so ``gate_message``'s challenge wording would mislead; a challenge is still recognised in
#: case the site switches one on.
_BLOCK_MSG = (
    "Cambridge Core served its bot gate instead of the item: open the article once in an "
    "authenticated browser tab on your off-campus session, then copy that session's cookies "
    "into [backends.cambridge] cookie / cookie_file — with the browser's own user_agent, and "
    "from the same machine")

#: Hosts the session cookies come from and a PDF may legitimately live on.
_COOKIE_HOSTS = ("cambridge",)
_ALLOWED_HOSTS = ("cambridge.org",)

#: ``cambridge:`` is the documented prefix; ``cambridge/`` is the ``source/key`` form the table
#: prints, which a pasted key can carry — and both stack on a ``doi:`` or ``doi.org`` form.
_DOI_PREFIX_RE = re.compile(
    r"^(?:(?:https?://(?:dx\.)?doi\.org/|doi:\s*|cambridge[:\/]\s*)\s*)+", re.I)
_DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$")
#: Any Cambridge Core page. Its path carries Cambridge's own product id, not the DOI, so a URL
#: key stays a URL until the page names its DOI in ``citation_doi``.
_URL_RE = re.compile(r"^https?://(?:www\.)?cambridge\.org(?:/core(?:/\S*)?)?$", re.I)
#: The PDF endpoint, ``content/view/<product id>/<file>.pdf`` (the file name is an internal
#: S-number for articles and a page-range name for chapters, and is not derivable from the DOI).
_PDF_PATH_RE = re.compile(
    r"""(?:https?://(?:www\.)?cambridge\.org)?(/core/services/aop-cambridge-core/content/view/"""
    r"""[A-Za-z0-9]+/[\w.-]+\.pdf)""", re.I)
#: The article licence block: its wording (and its ``creative-commons`` class) sits on the
#: open-access pages and on neither a paywalled page nor a book chapter. The bare
#: ``creativecommons.org`` link alone is not enough — related-article cards carry those too.
_OA_RE = re.compile(r"is an open access article|creative-commons__description", re.I)

_META_TAG_RE = re.compile(r"<meta\b[^>]*>", re.I)
_META_NAME_RE = re.compile(r"\bname=[\"']([^\"']+)[\"']", re.I)
_META_CONTENT_RE = re.compile(r"\bcontent=[\"']([^\"']*)[\"']", re.I)
_TITLE_TAG_RE = re.compile(r"<title\b[^>]*>(.*?)</title>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _unescaped_when_safe(url: str) -> str:
    return html_mod.unescape((url or "").replace("\\/", "/")).strip()


def _on_cambridge(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == s or host.endswith("." + s) for s in _ALLOWED_HOSTS)


# --- pure helpers (no network) ---

def doi_from(key: str) -> str:
    """Return a bare DOI from a DOI-shaped key (a ``doi.org``/``cambridge:`` form included)."""
    k = _DOI_PREFIX_RE.sub("", (key or "").strip()).strip()
    return k if _DOI_RE.match(k) else ""


def normalize_key(key: str) -> str:
    """Classify a key and return ``doi:<DOI>`` or ``url:<Cambridge Core URL>``.

    A pasted URL may percent-encode the DOI (or carry a query tail), which the DOI shapes here
    do not match, so a failing key is retried with and without decoding.
    """
    k = (key or "").strip()
    if k.lower().startswith("url:"):  # an already-normalized key keeps its kind
        k = k[len("url:"):].strip()
    k = re.sub(r"^(?:cambridge[:/]\s*)+", "", k, flags=re.I).strip()
    if not k:
        raise BadInput("[cambridge] empty key (expected a DOI, cambridge:<DOI> or Cambridge Core URL)")
    for candidate in (k, urllib.parse.unquote(k)) if "%" in k else (k,):
        if _URL_RE.match(candidate):
            return f"url:{candidate.split('#', 1)[0]}"
        doi = doi_from(candidate)
        if doi:
            return f"doi:{doi.rstrip('.')}"
    raise BadInput(f"[cambridge] not a DOI, cambridge:<DOI> or Cambridge Core URL: {key!r}")


def is_block_page(page_html: str, status: int = 200) -> bool:
    """True when the body is a bot/entitlement gate, or the status is a plain 403."""
    return is_challenge_page(page_html, status)


def is_open_access(page_html: str) -> bool:
    """True when the page carries an article licence (its PDF needs no session)."""
    return bool(_OA_RE.search(page_html or ""))


def is_short_page(url: str) -> bool:
    """True for the paywalled ``/abs/`` short page (a full page means OA or an entitled session)."""
    return "/abs/" in (urlparse(url).path or "")


def item_id(url: str) -> str:
    """The internal product id out of an item URL (its last path segment, when it is one)."""
    tail = (urlparse(url).path or "").rstrip("/").rsplit("/", 1)[-1]
    return tail if re.fullmatch(r"[A-Fa-f0-9]{32}", tail) else ""


def extract_pdf_url(page_html: str, want_id: str = "") -> str:
    """Return the item's PDF link, or "" when the page carries none.

    The page's own ``citation_pdf_url`` meta is authoritative; older/alternate markup carries a
    plain anchor instead, and related-article cards on the same page carry the same shape — so
    an anchor only counts when its path names this item's product id (``want_id``). The result
    must point at Cambridge — a third party here means the page handed back something
    unexpected; raising keeps that visible, while an empty return means "no link".
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
    if not raw:
        return ""
    match = _PDF_PATH_RE.search(raw)
    if match:  # keep the canonical short path, never the descriptive /div-class-title-… tail
        raw = match.group(1)
    if raw.startswith("//"):
        raw = "https:" + raw
    elif raw.startswith("/"):
        raw = _SITE + raw
    if not _on_cambridge(raw):
        host = urlparse(raw).hostname or raw
        raise RuntimeError(f"[cambridge] PDF link is not on a Cambridge host: {host}")
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
        "container": _text(first("citation_journal_title", "citation_inbook_title",
                                 "citation_book_title", "dc.source")),
        "volume": first("citation_volume"),
        "issue": first("citation_issue"),
        "page": _pages(first("citation_firstpage"), first("citation_lastpage"),
                       first("citation_pages")),
        "year": extract_year(first("citation_publication_date", "citation_online_date",
                                   "citation_date", "dc.date")),
        "doi": doi_from(first("citation_doi", "dc.identifier")),
        "abstract": _text(first("citation_abstract", "dc.description")),
        "publisher": _text(first("citation_publisher", "dc.publisher")),
    }


def page_to_item(page_html: str, key: str, url: str = "") -> AcademicItem:
    """Normalize an item page to an :class:`AcademicItem` (``citation_*`` → ``dc.*`` → title).

    ``key`` is the normalized key; the page's own ``citation_doi`` is the truth for ``doi``
    (the URL a ``url:`` key came from names only an internal product id).
    """
    page = page_html or ""
    fields = _fields(_metas(page))
    if not fields["title"]:
        title_tag = _TITLE_TAG_RE.search(page)
        if title_tag:
            title = _text(title_tag.group(1))
            fields["title"] = re.sub(r"\s*[|—-]\s*Cambridge Core\s*$", "", title).strip()
    doi = fields["doi"] or doi_from(key.split(":", 1)[-1])
    creators = [normalize_author_name(a) for a in fields["creators"]]
    return AcademicItem(
        source="cambridge",
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
        item_type="chapter" if "/books/" in (urlparse(url).path or "") else "article",
    )


def parse_cookie(raw: str) -> dict[str, str]:
    """Parse a cookie header / Netscape export, keeping only Cambridge rows."""
    return _parse_cookie(raw, _COOKIE_HOSTS)


def load_cookie(cookie: str = "", cookie_file: str = "") -> str:
    """The ``Cookie`` header value from an inline string and/or a cookie file (both merged)."""
    return _load_cookie(cookie, cookie_file, _COOKIE_HOSTS, "cambridge")


class CambridgeBackend(EntitledBackend):
    """Resolve a DOI / Cambridge Core URL to one item and, when entitled, its PDF link.

    A session you already entitled in your own browser is carried in as a ``Cookie`` header
    (``[backends.cambridge] .cookie``/``.cookie_file``) together with that browser's
    ``user_agent``. The page itself needs no session (metadata works anonymously); the PDF
    download does, and only for articles your institution subscribes to — open-access ones
    download without any session at all.
    """

    UA = _UA
    headers: ClassVar[dict] = {
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    min_interval = 1.0
    rate = 4
    COOKIE_HOSTS = _COOKIE_HOSTS
    GATE_NAME = "the Cambridge Core session cookie"
    BLOCK_MSG = _BLOCK_MSG

    def __init__(self, name: str = "cambridge", timeout: float = 30.0, delay: float = 1.0,
                 cookie: str = "", cookie_file: str = "", user_agent: str = "", **_unused):
        super().__init__(name=name, timeout=timeout, delay=delay, cookie=cookie,
                         cookie_file=cookie_file, user_agent=user_agent)
        self._last_key = self._last_alias = self._last_page = self._last_url = ""

    def _page(self, key: str) -> tuple[str, str]:
        """Fetch the item page as ``(html, landed url)``, turning a gate into one clear error.

        The page is memoized per key: the PDF download and the item read share one request. A
        DOI goes through ``doi.org`` and the redirect says what the item is — a paywalled one
        stops on its ``/abs/`` short page. The landed URL is kept because the product id in it
        is not derivable from the DOI.
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
        if not _on_cambridge(landed):
            raise NotFound(f"[{self.name}] DOI does not resolve to Cambridge Core: {normalized}")
        self._last_key, self._last_alias = normalized, ""
        self._last_page, self._last_url = response.text, landed
        return self._last_page, landed

    # -- Backend ---------------------------------------------------------------

    def search(self, query: str, limit: int = 10, filters: dict | None = None) -> list[AcademicItem]:
        """Resolve a DOI / URL query to one item; keyword queries return [].

        There is no key-free search endpoint, so anything that is not already an identifier
        cannot be looked up here. ``filters`` are not applied: there is at most one hit.
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
        """Fetch one item by DOI or Cambridge Core URL.

        A 404 is turned into :class:`NotFound` so a search treating the key as a candidate
        yields nothing instead of an error. The page names its PDF endpoint even anonymously,
        so it goes into ``attachments`` and the fulltext chain can try the bytes.
        """
        normalized = normalize_key(key)
        try:
            body, landed = self._page(normalized)
        except HttpError as e:
            if e.status in (404, 410):
                raise NotFound(f"[{self.name}] no such Cambridge Core item: {key}") from e
            raise
        item = page_to_item(body, normalized, landed)
        if normalized.startswith("url:") and item.doi:
            # The URL carried only an internal product id; the page named the real identity,
            # which also becomes the memo key so the PDF download reuses this page.
            item.key = f"doi:{item.doi}"
            self._last_alias = f"doi:{item.doi}"
        if not item.title:
            raise RuntimeError(f"[{self.name}] could not parse the item page: {landed}")
        try:
            pdf_url = extract_pdf_url(body, item_id(landed))
        except RuntimeError:  # an odd host must not cost the metadata
            pdf_url = ""
        if pdf_url:
            item.attachments = [pdf_url]
        if is_open_access(body):
            # The chain labels the hit from this: an open-access item is not a subscription
            # copy, so its note and its "needs a session" advice would both be wrong.
            item.extra = {**item.extra, "open_access": True}
            item.access = provenance.OA
        return item

    def get_attachment(self, key: str) -> bytes:
        """Download the item PDF (an entitled session, or open access, serves it)."""
        normalized = normalize_key(key)
        page, landed = self._page(normalized)
        url = extract_pdf_url(page, item_id(landed))
        if not url:
            raise Unavailable(f"[{self.name}] the page carries no PDF link: {landed}")
        open_access = is_open_access(page)
        try:
            response = self.fetch(url, headers={"Accept": "application/pdf"})
        except HttpError as e:
            if is_block_page(e.body, e.status):
                raise Unavailable(f"[{self.name}] PDF download was gated: {_BLOCK_MSG}") from e
            raise
        if response.content.startswith(b"%PDF"):
            return response.content
        # An anonymous or unentitled session is sent back to the item page (HTML, never a 403).
        short = is_short_page(landed)
        if open_access:
            why = "the item is open access but the endpoint answered an HTML page"
        elif short:
            why = ("the item answers only its /abs/ short page, so the session is not entitled "
                   "to it (missing, expired, or not subscribed to this title)")
        else:
            why = "the endpoint answered an HTML page instead of the PDF"
        raise Unavailable(
            f"[{self.name}] no PDF bytes: {why} (landed on {landed}) — open {url} in your "
            "entitled browser, or refresh the cookie+user_agent pair from an authenticated tab")
