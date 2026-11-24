"""AIP Publishing reader (pubs.aip.org), read-only.

A DOI resolves through ``doi.org`` to ``pubs.aip.org`` (the legacy ``aip.scitation.org`` host
still exists for old links). The site sits behind a Cloudflare *managed challenge*: a plain
HTTP client is answered with ``403`` and ``cf-mitigated: challenge`` (the "Just a moment…"
page) instead of the article, on the page and on the ``article-pdf`` endpoint alike. The
backend therefore carries in the cookies of a session the user already cleared in their own
browser (``[backends.aip] .cookie``/``.cookie_file`` plus that browser's own ``.user_agent``,
which the ``cf_clearance`` cookie is bound to) and names the gate when they are missing.

Metadata comes from the served page's ``citation_*`` tags, and the PDF link from their
``citation_pdf_url`` or an ``/article-pdf/<id>/…`` anchor; ``pubs.aip.org/doi/pdf/<DOI>`` is the
URL rewrite of last resort. A body that is not ``%PDF`` is reported with what actually happened. The shared
shape (key normalization, page parse, PDF pick, refusal message, and the gate itself) lives in
:mod:`._gated`.

``capabilities`` is deliberately ``{"read"}`` only (like ScienceDirect / Wiley / Springer /
Cambridge) so the fulltext chain never mistakes an AIP hit for a local-store attachment.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

from ..models import AcademicItem
from ._cf_gate import is_challenge_page
from ._cf_gate import load_cookie as _load_cookie
from ._cf_gate import parse_cookie as _parse_cookie
from ._gated import OA_RE as _OA_RE
from ._gated import GatedBackend
from ._gated import extract_pdf_url as _extract_pdf_url
from ._gated import normalize_key as _normalize_key
from ._gated import page_to_item as _page_to_item

_SITE = "https://pubs.aip.org"

#: Hosts the session cookies come from and the PDF may legitimately live on.
_COOKIE_HOSTS = ("pubs.aip.org", "aip.org", "scitation.org")
_ALLOWED_HOSTS = ("aip.org", "scitation.org")

#: Any AIP page. The modern path carries an internal numeric id, not the DOI, so a URL key
#: stays a URL until the page names its own DOI in ``citation_doi``.
_URL_RE = re.compile(r"^https?://(?:pubs\.aip\.org|aip\.scitation\.org)(?:/\S*)?$", re.I)
#: A DOI embedded in a legacy ``/doi/<DOI>``, ``/doi/abs/<DOI>``, ``/doi/pdf/<DOI>`` URL.
_URL_DOI_RE = re.compile(r"/doi/(?:abs/|full/|pdf/|epdf/)?(10\.\d{4,9}/[^?#\s]+)", re.I)
#: The article's PDF endpoint: ``/<journal>/article-pdf/<numeric id>/<n>/<slug>``. Leading
#: segments vary (a bare ``/article-pdf/…`` anchor resolves too), so they are matched as a run.
_PDF_PATH_RE = re.compile(
    r"""((?:/[A-Za-z0-9_-]+)*/article-pdf/[A-Za-z0-9]+/[^"'?#\s]+)""", re.I)

_KEY_FORMS = "a DOI, aip:<DOI> or AIP article URL"


def _on_aip(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == s or host.endswith("." + s) for s in _ALLOWED_HOSTS)


def normalize_key(key: str) -> str:
    """Classify a key and return ``doi:<DOI>`` or ``url:<AIP URL>``."""
    return _normalize_key(key, tag="aip", url_re=_URL_RE, forms=_KEY_FORMS,
                          embedded_doi_re=_URL_DOI_RE)


def is_block_page(page_html: str, status: int = 200) -> bool:
    """True when the body is the Cloudflare challenge page, or the status is a plain 403."""
    return is_challenge_page(page_html, status)


def is_open_access(page_html: str) -> bool:
    """True when the page carries an article licence (its PDF needs no session)."""
    return bool(_OA_RE.search(page_html or ""))


def extract_pdf_url(page_html: str, doi: str = "", want_id: str = "") -> str:
    """Return the article's PDF link, or "" when the page carries none."""
    return _extract_pdf_url(page_html, doi, want_id, site=_SITE, pdf_path_re=_PDF_PATH_RE,
                            template=_SITE + "/doi/pdf/{doi}", on_site=_on_aip, tag="aip")


def page_to_item(page_html: str, key: str, url: str = "") -> AcademicItem:
    """Normalize an article page to an :class:`AcademicItem`."""
    return _page_to_item(page_html, key, url, source="aip", title_suffix="AIP Publishing")


def parse_cookie(raw: str) -> dict[str, str]:
    """Parse a cookie header / Netscape export, keeping only AIP rows."""
    return _parse_cookie(raw, _COOKIE_HOSTS)


def load_cookie(cookie: str = "", cookie_file: str = "") -> str:
    """The cookie string from an inline string and/or a cookie file (both merged)."""
    return _load_cookie(cookie, cookie_file, _COOKIE_HOSTS, "aip")


class AipBackend(GatedBackend):
    """Resolve a DOI / AIP article URL to one item and, when entitled, its PDF link.

    The site is behind a Cloudflare managed challenge, so a session you already cleared in
    your own browser is carried in as a cookie string (``[backends.aip]
    .cookie``/``.cookie_file``) together with that browser's ``user_agent``, and requests look
    like that browser's session. Without one, both the page and the PDF answer a gate.
    """

    DEFAULT_NAME = "aip"
    SITE = _SITE
    SITE_NAME = "AIP Publishing"
    GATE_NAME = "the AIP clearance cookie"
    KEY_PREFIX = "aip"
    KEY_FORMS = _KEY_FORMS
    TITLE_SUFFIX = "AIP Publishing"
    ALLOWED_HOSTS = _ALLOWED_HOSTS
    COOKIE_HOSTS = _COOKIE_HOSTS
    URL_RE = _URL_RE
    EMBEDDED_DOI_RE = _URL_DOI_RE
    PDF_PATH_RE = _PDF_PATH_RE
    PDF_URL_TEMPLATE = _SITE + "/doi/pdf/{doi}"
    #: The id is the numeric segment before the trailing slug
    #: (``/pop/article/1/12/4022/260756/local-modes…``): the path pieces before it are journal
    #: and pagination, so anchoring on ``/article/`` alone loses the modern shape.
    ITEM_ID_RE = re.compile(r"/(\d+)/[^/]+/?$")
    OA_RE = _OA_RE
