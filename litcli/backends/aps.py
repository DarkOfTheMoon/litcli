"""APS reader (link.aps.org), read-only.

The site is behind a Cloudflare managed challenge (verified 2026-11-23: the page and the
``/doi/pdf/`` endpoint both answer ``403`` with ``cf-mitigated: challenge``), so the backend
carries in the cookies of a cleared browser session (``[backends.aps] .cookie``/
``.cookie_file`` plus that browser's own ``.user_agent``) and names the gate when they are
missing.

A key is a DOI, ``aps:<DOI>`` or a ``link.aps.org/doi/…`` URL (the path carries the DOI).
Open-access articles carry the APS open-access licence wording and are marked as such. The
shared shape lives in :mod:`._gated`.
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

_SITE = "https://link.aps.org"
_COOKIE_HOSTS = ("link.aps.org", "aps.org")
_ALLOWED_HOSTS = ("aps.org",)

_URL_RE = re.compile(r"^https?://link\.aps\.org/\S*$", re.I)
_URL_DOI_RE = re.compile(r"/doi/(?:abs/|full/|pdf/|epdf/)?(10\.\d{4,9}/[^?#\s]+)", re.I)
_PDF_PATH_RE = re.compile(r"""(/doi/(?:pdf|epdf)/10\.\d{4,9}/[^"'?#\s]+)""", re.I)

_KEY_FORMS = "a DOI, aps:<DOI> or APS article URL"


def _on_aps(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == s or host.endswith("." + s) for s in _ALLOWED_HOSTS)


def normalize_key(key: str) -> str:
    """Classify a key and return ``doi:<DOI>`` or ``url:<APS URL>``."""
    return _normalize_key(key, tag="aps", url_re=_URL_RE, forms=_KEY_FORMS,
                          embedded_doi_re=_URL_DOI_RE)


def is_block_page(page_html: str, status: int = 200) -> bool:
    """True when the body is the Cloudflare challenge page, or the status is a plain 403."""
    return is_challenge_page(page_html, status)


def is_open_access(page_html: str) -> bool:
    """True when the page carries the CC licence block (its PDF needs no session)."""
    return bool(_OA_RE.search(page_html or ""))


def extract_pdf_url(page_html: str, doi: str = "", want_id: str = "") -> str:
    """Return the item's PDF link, or "" when the page carries none."""
    return _extract_pdf_url(page_html, doi, want_id, site=_SITE, pdf_path_re=_PDF_PATH_RE,
                            template=_SITE + "/doi/pdf/{doi}", on_site=_on_aps, tag="aps")


def page_to_item(page_html: str, key: str, url: str = "") -> AcademicItem:
    """Normalize an item page to an :class:`AcademicItem`."""
    return _page_to_item(page_html, key, url, source="aps", title_suffix="APS")


def parse_cookie(raw: str) -> dict[str, str]:
    """Parse a cookie header / Netscape export, keeping only APS rows."""
    return _parse_cookie(raw, _COOKIE_HOSTS)


def load_cookie(cookie: str = "", cookie_file: str = "") -> str:
    """The cookie string from an inline string and/or a cookie file (both merged)."""
    return _load_cookie(cookie, cookie_file, _COOKIE_HOSTS, "aps")


class ApsBackend(GatedBackend):
    """Resolve a DOI / APS URL to one item and, when entitled, its PDF link."""

    DEFAULT_NAME = "aps"
    SITE = _SITE
    SITE_NAME = "APS"
    GATE_NAME = "the APS clearance cookie"
    KEY_PREFIX = "aps"
    KEY_FORMS = _KEY_FORMS
    TITLE_SUFFIX = "APS"
    ALLOWED_HOSTS = _ALLOWED_HOSTS
    COOKIE_HOSTS = _COOKIE_HOSTS
    URL_RE = _URL_RE
    EMBEDDED_DOI_RE = _URL_DOI_RE
    PDF_PATH_RE = _PDF_PATH_RE
    PDF_URL_TEMPLATE = _SITE + "/doi/pdf/{doi}"
    OA_RE = _OA_RE
