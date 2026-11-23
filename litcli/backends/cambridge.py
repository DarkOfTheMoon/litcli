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
the fulltext chain never mistakes a Cambridge hit for a local-store attachment. The shared shape
lives in :mod:`._gated`.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

from ..models import AcademicItem
from ._cf_gate import is_challenge_page
from ._gated import GatedBackend
from ._gated import extract_pdf_url as _extract_pdf_url
from ._gated import load_cookie as _load_cookie
from ._gated import normalize_key as _normalize_key
from ._gated import page_to_item as _page_to_item
from ._gated import parse_cookie as _parse_cookie

_SITE = "https://www.cambridge.org"

#: Cambridge is not Cloudflare-walled today (it only sets a ``__cf_bm`` bot-management cookie),
#: so ``gate_message``'s challenge wording would mislead; a challenge is still recognised in
#: case the site switches one on.
_BLOCK_MSG = (
    "Cambridge Core served its bot gate instead of the item: open the article once in an "
    "authenticated browser tab on your off-campus session, then copy that session's cookies "
    "into [backends.cambridge] cookie / cookie_file — with the browser's own user_agent, and "
    "from the same machine")

#: Hosts the session cookies come from and a PDF may legitimately live on.
_COOKIE_HOSTS = ("www.cambridge.org", "cambridge.org")
_ALLOWED_HOSTS = ("cambridge.org",)

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

_KEY_FORMS = "a DOI, cambridge:<DOI> or Cambridge Core URL"


def _on_cambridge(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == s or host.endswith("." + s) for s in _ALLOWED_HOSTS)


def normalize_key(key: str) -> str:
    """Classify a key and return ``doi:<DOI>`` or ``url:<Cambridge Core URL>``."""
    return _normalize_key(key, tag="cambridge", url_re=_URL_RE, forms=_KEY_FORMS)


def is_block_page(page_html: str, status: int = 200) -> bool:
    """True when the body is a bot/entitlement gate, or the status is a plain 403."""
    return is_challenge_page(page_html, status)


def is_open_access(page_html: str) -> bool:
    """True when the page carries an article licence (its PDF needs no session)."""
    return bool(_OA_RE.search(page_html or ""))


def is_short_page(url: str) -> bool:
    """True for the paywalled ``/abs/`` short page (a full page means OA or an entitled session)."""
    return "/abs/" in (urlparse(url).path or "")


def extract_pdf_url(page_html: str, doi: str = "", want_id: str = "") -> str:
    """Return the item's PDF link, or "" when the page carries none."""
    return _extract_pdf_url(page_html, doi, want_id, site=_SITE, pdf_path_re=_PDF_PATH_RE,
                            template="", on_site=_on_cambridge, tag="cambridge")


def page_to_item(page_html: str, key: str, url: str = "") -> AcademicItem:
    """Normalize an item page to an :class:`AcademicItem`."""
    return _page_to_item(page_html, key, url, source="cambridge", title_suffix="Cambridge Core",
                         item_type=lambda u, _k: "chapter" if "/books/" in (urlparse(u).path or "")
                         else "article")


def parse_cookie(raw: str) -> dict[str, str]:
    """Parse a cookie header / Netscape export, keeping only Cambridge rows."""
    return _parse_cookie(raw, _COOKIE_HOSTS)


def load_cookie(cookie: str = "", cookie_file: str = "") -> str:
    """The cookie string from an inline string and/or a cookie file (both merged)."""
    return _load_cookie(cookie, cookie_file, _COOKIE_HOSTS, "cambridge")


class CambridgeBackend(GatedBackend):
    """Resolve a DOI / Cambridge Core URL to one item and, when entitled, its PDF link.

    A session you already entitled in your own browser is carried in as a cookie string
    (``[backends.cambridge] .cookie``/``.cookie_file``) together with that browser's
    ``user_agent``. The page itself needs no session (metadata works anonymously); the PDF
    download does, and only for articles your institution subscribes to — open-access ones
    download without any session at all.
    """

    DEFAULT_NAME = "cambridge"
    SITE = _SITE
    SITE_NAME = "Cambridge Core"
    GATE_NAME = "the Cambridge Core session cookie"
    #: Its page (and PDF link) answers anonymously; only the bytes want a session.
    ANONYMOUS_PAGES = True
    BLOCK_MSG = _BLOCK_MSG
    KEY_PREFIX = "cambridge"
    KEY_FORMS = _KEY_FORMS
    TITLE_SUFFIX = "Cambridge Core"
    ALLOWED_HOSTS = _ALLOWED_HOSTS
    COOKIE_HOSTS = _COOKIE_HOSTS
    URL_RE = _URL_RE
    PDF_PATH_RE = _PDF_PATH_RE
    ITEM_ID_RE = re.compile(r"/([A-Fa-f0-9]{32})/?$")
    OA_RE = _OA_RE

    def _short_page(self, url: str) -> bool:
        return is_short_page(url)

    def _refusal_reason(self, page: str, landed: str, response) -> str:
        if self.is_open_access(page):
            return "the item is open access but the endpoint answered an HTML page"
        if self._short_page(landed):
            return ("the item answers only its /abs/ short page, so the session is not entitled "
                    "to it (missing, expired, or not subscribed to this title)")
        return "the endpoint answered an HTML page instead of the PDF"
