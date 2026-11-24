"""IEEE Xplore reader (ieeexplore.ieee.org), read-only.

The site answers a plain HTTP client with an **AWS WAF** JavaScript challenge (verified
2026-11-23: an empty ``202`` body carrying ``window.gokuProps``), so the backend carries in
the cookies of a session the user already passed in their own browser — chiefly
``aws-waf-token`` — together with that browser's own ``.user_agent``, which the token is bound
to (``[backends.ieee] .cookie``/``.cookie_file``).

A key is a DOI or an ``ieeexplore.ieee.org/document/<arnumber>`` URL; unlike the DOI-in-path
publishers the URL carries an internal article number, so a URL key stays a URL until the page
names its DOI in ``citation_doi``. The PDF endpoint is
``/stampPDF/getPDF.jsp?tp=&arnumber=<id>``, and a session that is not entitled is redirected
to ``/Xplore/login.jsp?…&authDecision=…`` (an HTML body, never a 403). The shared shape lives
in :mod:`._gated`.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

from ..models import AcademicItem
from ._cf_gate import load_cookie as _load_cookie
from ._cf_gate import parse_cookie as _parse_cookie
from ._gated import GatedBackend
from ._gated import extract_pdf_url as _extract_pdf_url
from ._gated import is_open_access as is_open_access
from ._gated import normalize_key as _normalize_key
from ._gated import page_to_item as _page_to_item

_SITE = "https://ieeexplore.ieee.org"
_COOKIE_HOSTS = ("ieeexplore.ieee.org", "ieee.org")
_ALLOWED_HOSTS = ("ieeexplore.ieee.org",)

_URL_RE = re.compile(r"^https?://ieeexplore\.ieee\.org/\S*$", re.I)
_PDF_PATH_RE = re.compile(
    r"""(/(?:stampPDF/getPDF\.jsp|stamp/stamp\.jsp)\?tp=&arnumber=\d+)""", re.I)
#: The badge class is what counts, not the words: Xplore's nav says "Open Access" on every
#: page. The shared marker set carries that class token (see :data:`._gated.OA_MARKERS`).

_KEY_FORMS = "a DOI, ieee:<DOI> or IEEE Xplore article URL"


def _on_ieee(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == s or host.endswith("." + s) for s in _ALLOWED_HOSTS)


def normalize_key(key: str) -> str:
    """Classify a key and return ``doi:<DOI>`` or ``url:<IEEE Xplore URL>``."""
    return _normalize_key(key, tag="ieee", url_re=_URL_RE, forms=_KEY_FORMS)


def extract_pdf_url(page_html: str, doi: str = "", want_id: str = "") -> str:
    """Return the item's PDF link, or "" when the page carries none."""
    return _extract_pdf_url(page_html, doi, want_id, site=_SITE, pdf_path_re=_PDF_PATH_RE,
                            template=_SITE + "/stampPDF/getPDF.jsp?tp=&arnumber={id}",
                            on_site=_on_ieee, tag="ieee")


def page_to_item(page_html: str, key: str, url: str = "") -> AcademicItem:
    """Normalize an item page to an :class:`AcademicItem`."""
    return _page_to_item(page_html, key, url, source="ieee", title_suffix="IEEE Xplore")


def parse_cookie(raw: str) -> dict[str, str]:
    """Parse a cookie header / Netscape export, keeping only IEEE rows."""
    return _parse_cookie(raw, _COOKIE_HOSTS)


def load_cookie(cookie: str = "", cookie_file: str = "") -> str:
    """The cookie string from an inline string and/or a cookie file (both merged)."""
    return _load_cookie(cookie, cookie_file, _COOKIE_HOSTS, "ieee")


class IeeeBackend(GatedBackend):
    """Resolve a DOI / IEEE Xplore URL to one item and, when entitled, its PDF link."""

    DEFAULT_NAME = "ieee"
    SITE = _SITE
    SITE_NAME = "IEEE Xplore"
    GATE_KIND = "waf"
    GATE_NAME = "the IEEE Xplore AWS WAF token cookie"
    KEY_PREFIX = "ieee"
    KEY_FORMS = _KEY_FORMS
    TITLE_SUFFIX = "IEEE Xplore"
    ALLOWED_HOSTS = _ALLOWED_HOSTS
    COOKIE_HOSTS = _COOKIE_HOSTS
    URL_RE = _URL_RE
    PDF_PATH_RE = _PDF_PATH_RE
    PDF_URL_TEMPLATE = _SITE + "/stampPDF/getPDF.jsp?tp=&arnumber={id}"
    ITEM_ID_RE = re.compile(r"(?:/document/|arnumber=)(\d+)")

    def _not_entitled_reason(self, landed: str, response) -> str:
        # UNVERIFIED: Xplore has no distinct short-page URL to gate on (no ``_short_page``), so a
        # related-card badge on a paywalled document is indistinguishable from the page's own
        # marker here. Checked against a real paywalled Xplore page carrying an OA neighbour.
        if "login.jsp" in str(response.url):
            return ("the endpoint redirected to the Xplore login page, so the session is not "
                    "entitled to this item (missing, expired, or not subscribed)")
        return "the session is not entitled to this item (expired, or not subscribed)"
