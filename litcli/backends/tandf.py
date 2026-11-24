"""Taylor & Francis Online reader (www.tandfonline.com), read-only.

The site is behind a Cloudflare managed challenge (verified 2026-11-23: the article page and
the ``/doi/pdf/`` endpoint both answer ``403`` with ``cf-mitigated: challenge``), so the
backend carries in the cookies of a cleared browser session (``[backends.tandf] .cookie``/
``.cookie_file`` plus that browser's own ``.user_agent``) and names the gate when they are
missing.

A key is a DOI or a ``tandfonline.com/doi/…`` URL (the path carries the DOI). Open-access
articles carry the "This is an Open Access article … Creative Commons" block and are marked as
such. The shared shape lives in :mod:`._gated`.
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

_SITE = "https://www.tandfonline.com"
_COOKIE_HOSTS = ("www.tandfonline.com", "tandfonline.com")
_ALLOWED_HOSTS = ("tandfonline.com",)

_URL_RE = re.compile(r"^https?://(?:www\.)?tandfonline\.com/\S*$", re.I)
_URL_DOI_RE = re.compile(r"/doi/(?:full/|abs/|pdf/|epdf/)?(10\.\d{4,9}/[^?#\s]+)", re.I)
_PDF_PATH_RE = re.compile(r"""(/doi/(?:pdf|epdf)/10\.\d{4,9}/[^"'?#\s]+)""", re.I)

_KEY_FORMS = "a DOI, tandf:<DOI> or Taylor & Francis URL"


def _on_tandf(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == s or host.endswith("." + s) for s in _ALLOWED_HOSTS)


def normalize_key(key: str) -> str:
    """Classify a key and return ``doi:<DOI>`` or ``url:<Taylor & Francis URL>``."""
    return _normalize_key(key, tag="tandf", url_re=_URL_RE, forms=_KEY_FORMS,
                          embedded_doi_re=_URL_DOI_RE)



def extract_pdf_url(page_html: str, doi: str = "", want_id: str = "") -> str:
    """Return the item's PDF link, or "" when the page carries none."""
    return _extract_pdf_url(page_html, doi, want_id, site=_SITE, pdf_path_re=_PDF_PATH_RE,
                            template=_SITE + "/doi/pdf/{doi}", on_site=_on_tandf, tag="tandf")


def page_to_item(page_html: str, key: str, url: str = "") -> AcademicItem:
    """Normalize an item page to an :class:`AcademicItem`."""
    return _page_to_item(page_html, key, url, source="tandf",
                         title_suffix="Taylor & Francis Online")


def parse_cookie(raw: str) -> dict[str, str]:
    """Parse a cookie header / Netscape export, keeping only Taylor & Francis rows."""
    return _parse_cookie(raw, _COOKIE_HOSTS)


def load_cookie(cookie: str = "", cookie_file: str = "") -> str:
    """The cookie string from an inline string and/or a cookie file (both merged)."""
    return _load_cookie(cookie, cookie_file, _COOKIE_HOSTS, "tandf")


class TandfBackend(GatedBackend):
    """Resolve a DOI / Taylor & Francis URL to one item and, when entitled, its PDF link."""

    DEFAULT_NAME = "tandf"
    SITE = _SITE
    SITE_NAME = "Taylor & Francis Online"
    GATE_NAME = "the Taylor & Francis clearance cookie"
    KEY_PREFIX = "tandf"
    KEY_FORMS = _KEY_FORMS
    TITLE_SUFFIX = "Taylor & Francis Online"
    ALLOWED_HOSTS = _ALLOWED_HOSTS
    COOKIE_HOSTS = _COOKIE_HOSTS
    URL_RE = _URL_RE
    EMBEDDED_DOI_RE = _URL_DOI_RE
    PDF_PATH_RE = _PDF_PATH_RE
    PDF_URL_TEMPLATE = _SITE + "/doi/pdf/{doi}"
