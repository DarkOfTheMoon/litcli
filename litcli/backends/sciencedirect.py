"""ScienceDirect article reader (sciencedirect.com), read-only.

ScienceDirect has no key-free search API: the site's search UI is JavaScript-driven and the
API (``api.elsevier.com``) needs a key. What *is* fetchable without one is the article page
itself, so this backend resolves a known identifier (DOI, PII or article URL) to one item
and, when the session is entitled, to the article's PDF link.

Whether the PDF actually comes back depends on the caller's session: a subscribed, on-campus,
VPN or logged-in client gets it, an anonymous one gets a Cloudflare challenge page instead
(the one that carries ``CPE00001`` / "Are you a robot"). The backend does not log in; it accepts the cookies of a
session you already entitled in your browser (``[backends.sciencedirect] .cookie``/`.cookie_file`,
inline or Netscape ``cookies.txt``), and names what happened when they are missing or stale.

``capabilities`` is deliberately ``{"read"}`` only (like Sci-Hub) so the fulltext chain never
mistakes a ScienceDirect hit for a local-store attachment.

The markup is parsed by pure functions (:func:`normalize_key`, :func:`page_to_item`,
:func:`extract_pdf_url`, :func:`is_block_page`) so a site change touches only the thin
adapter, and every branch is testable offline with a literal page snippet.
"""
from __future__ import annotations

import html as html_mod
import json
import re
import urllib.parse
from typing import ClassVar
from urllib.parse import urlparse

from ..errors import BadInput, NotFound, Unavailable, for_status
from ..models import AcademicItem, extract_year, normalize_author_name
from ..transport import HttpError
from ._cf_gate import gate_message, is_challenge_page
from ._cf_gate import load_cookie as _load_cookie
from ._cf_gate import parse_cookie as _parse_cookie
from ._entitled import EntitledBackend

_SITE = "https://www.sciencedirect.com"
_ARTICLE = f"{_SITE}/science/article"

# A browser-ish UA: the site treats obviously scripted clients more harshly.
_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/120.0 Safari/537.36 (litcli)")

_BLOCK_MSG = gate_message("ScienceDirect", "sciencedirect")

_DOI_PREFIX_RE = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", re.I)
_DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$")
#: A bare PII: "S" + digits (an "X" checksum may appear) + more digits. Elsevier prints the
#: separators in URLs (``S0045-7825(05)00073-3``) and PIIs are also written without them.
_PII_RE = re.compile(r"^S[\dX]{4,}[0-9X]\d+$", re.I)
#: A PII embedded in an article / linkinghub URL (``/pii/<ID>``, ``/retrieve/pii/<ID>``).
_URL_PII_RE = re.compile(r"(?:/pii/|/retrieve/pii/)([A-Za-z0-9()\-]+)", re.I)
_NON_ALNUM_RE = re.compile(r"[^A-Za-z0-9]")


def _canonical_pii(raw: str) -> str:
    """A PII with the URL separators stripped (``S0045-7825(05)00073-3`` → ``S0045782505000733``)."""
    return _NON_ALNUM_RE.sub("", raw or "").upper()

#: Hosts an Elsevier-hosted PDF may legitimately live on.
_ALLOWED_HOSTS = ("sciencedirect.com", "sciencedirectassets.com",
                  "elsevier.com", "elsevierhealth.com")

_LD_RE = re.compile(
    r'<script\b[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.S | re.I)
_PDFURL_RE = re.compile(r"""pdfUrl["']?\s*[:=]\s*["']([^"']+)["']""", re.I)
#: The PDF anchor a served article page actually carries: ``/science/article/pii/<PII>/pdfft``
#: with a session-scoped md5 token. Related-article cards on the same page carry it too, hence
#: the PII captured alongside so only the article's own link is taken.
_PDFFT_RE = re.compile(
    r"""["'](/?science/article/pii/([A-Za-z0-9()\-]+)/pdfft\?md5=[0-9a-f]{32}&(?:amp;)?pid=[^"']+)["']""",
    re.I)
#: The PDF itself sits behind a second Elsevier acceptance gate (``craft/challenge/pdf``,
#: ``cra-ch-clearance``) that only a browser clears; a page-level session is not enough.
_PDF_GATE_MSG = ("the PDF sits behind a second ScienceDirect acceptance gate that only a "
                 "browser clears: open the article in your entitled browser and download the "
                 "PDF from there")
_META_TAG_RE = re.compile(r"<meta\b[^>]*>", re.I)
_META_NAME_RE = re.compile(r"\bname=[\"']([^\"']+)[\"']", re.I)
_META_CONTENT_RE = re.compile(r"\bcontent=[\"']([^\"']*)[\"']", re.I)
_TITLE_TAG_RE = re.compile(r"<title\b[^>]*>(.*?)</title>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


# --- pure helpers (no network) ---

def doi_from(key: str) -> str:
    """Return a bare DOI from a DOI-shaped key (or a ``doi.org`` URL), else ""."""
    k = (key or "").strip()
    k = _DOI_PREFIX_RE.sub("", k).strip()
    return k if _DOI_RE.match(k) else ""


def normalize_key(key: str) -> str:
    """Classify a key and return ``pii:<ID>`` (upper-cased) or ``doi:<DOI>``.

    Accepts a DOI, ``PII:<id>``, a bare PII, or a full ScienceDirect / linkinghub article
    URL. A DOI needs one network hop (``https://doi.org/<doi>``) before a PII is known, so
    it stays a distinct form here; :meth:`ScienceDirectBackend.get` resolves it.
    """
    k = (key or "").strip()
    if not k:
        raise BadInput("[sciencedirect] empty key")
    for prefix in ("sciencedirect/", "sciencedirect:"):
        if k.lower().startswith(prefix):
            k = k[len(prefix):].strip()
    embedded = _URL_PII_RE.search(k)
    if embedded:
        return f"pii:{_canonical_pii(embedded.group(1))}"
    if k.lower().startswith("pii:"):
        k = k[len("pii:"):].strip()
    if _PII_RE.match(_canonical_pii(k)):
        return f"pii:{_canonical_pii(k)}"
    doi = doi_from(k)
    if doi:
        return f"doi:{doi}"
    raise BadInput(f"[sciencedirect] not a DOI, PII or ScienceDirect URL: {key!r}")


#: The module-local name for the shared gate predicate (kept: callers and tests use it).
is_block_page = is_challenge_page


def _unescaped_when_safe(url: str) -> str:
    return html_mod.unescape((url or "").replace("\\/", "/")).strip()


def _on_elsevier(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == s or host.endswith("." + s) for s in _ALLOWED_HOSTS)


def extract_pdf_url(page_html: str, pii: str = "") -> str:
    """Return the article's PDF link, or "" when the page carries none.

    A served article page carries no ``pdfUrl`` key: an entitled (or open-archive) article
    shows a ``pdfft?md5=…&pid=…`` anchor instead, whose md5 is a short-lived session token.
    Older/alternate markup may still carry ``pdfUrl`` (JSON or a JS literal, escaped as
    ``\\/``) or a ``citation_pdf_url`` meta; those win when present. Related-article cards on
    the same page carry ``pdfft`` links as well, so when ``pii`` is given only a link naming
    that PII counts. The result must point at an Elsevier host — a third party here means the
    page handed back something unexpected. Raising on a bad host keeps that visible; an empty
    return means "not entitled / needs a login".
    """
    text = page_html or ""
    raw = ""
    for candidate in _PDFURL_RE.findall(text):
        raw = _unescaped_when_safe(candidate)
        if raw:
            break
    if not raw:
        raw = _unescaped_when_safe((_metas(text).get("citation_pdf_url") or [""])[0])
    if not raw:
        want = _canonical_pii(pii)
        first = ""
        for candidate, link_pii in _PDFFT_RE.findall(text):
            if not first:
                first = candidate
            if not want or _canonical_pii(link_pii) == want:
                raw = _unescaped_when_safe(candidate)
                break
        if not raw and not want:
            raw = _unescaped_when_safe(first)
    if not raw:
        return ""
    if raw.startswith("//"):
        raw = "https:" + raw
    elif raw.startswith("/"):
        raw = _SITE + raw
    if not _on_elsevier(raw):
        host = urlparse(raw).hostname or raw
        raise RuntimeError(f"[sciencedirect] PDF link is not on an Elsevier host: {host}")
    return raw


def _text(value) -> str:
    """Strip tags/entities and collapse whitespace."""
    return _WS_RE.sub(" ", html_mod.unescape(_TAG_RE.sub(" ", str(value or "")))).strip()


def _as_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


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


def _json_ld(page_html: str):
    """Yield each JSON-LD record that describes a scholarly article."""
    for raw in _LD_RE.findall(page_html or ""):
        try:
            data = json.loads(raw.strip())
        except ValueError:
            continue
        for rec in _as_list(data):
            if not isinstance(rec, dict):
                continue
            types = [str(t).lower() for t in _as_list(rec.get("@type"))]
            if any("article" in t for t in types):
                yield rec


def _pages(first: str, last: str, single: str) -> str:
    if first and last and first != last:
        return f"{first}-{last}"
    return single or first


def _ld_fields(rec: dict) -> dict:
    """Read the fields we keep out of one JSON-LD article record."""
    authors = []
    for author in _as_list(rec.get("author")):
        if isinstance(author, dict):
            name = author.get("name") or " ".join(
                p for p in (author.get("givenName"), author.get("familyName")) if p)
        else:
            name = str(author)
        if name and str(name).strip():
            authors.append(str(name))
    part = rec.get("isPartOf") if isinstance(rec.get("isPartOf"), dict) else {}
    identifier = rec.get("identifier") or rec.get("doi") or ""
    if isinstance(identifier, dict):
        identifier = identifier.get("value", "")
    publisher = rec.get("publisher")
    if isinstance(publisher, dict):
        publisher = publisher.get("name", "")
    return {
        "title": _text(rec.get("headline") or rec.get("name")),
        "creators": authors,
        "container": _text(part.get("name")),
        "volume": str(part.get("volumeNumber") or ""),
        "issue": str(part.get("issueNumber") or ""),
        "page": _pages(str(rec.get("pageStart") or ""), str(rec.get("pageEnd") or ""),
                       str(rec.get("pagination") or "")),
        "year": extract_year(str(rec.get("datePublished") or "")),
        "doi": str(identifier or ""),
        "abstract": _text(rec.get("description")),
        "publisher": str(publisher or ""),
    }


def _meta_fields(metas: dict[str, list[str]]) -> dict:
    """Read the same fields out of ``citation_*`` meta, falling back to ``dc.*``."""
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
                                 "dc.source", "dc.journal")),
        "volume": first("citation_volume"),
        "issue": first("citation_issue"),
        "page": _pages(first("citation_firstpage"), first("citation_lastpage"),
                       first("citation_pages", "citation_page")),
        "year": extract_year(first("citation_publication_date", "citation_date",
                                   "citation_year", "dc.date")),
        "doi": first("citation_doi", "dc.identifier"),
        "abstract": _text(first("citation_abstract", "dc.description")),
        "publisher": first("citation_publisher", "dc.publisher"),
    }


def page_to_item(page_html: str, key: str) -> AcademicItem:
    """Parse an article page into an item; ``key`` must be ``pii:<ID>``.

    JSON-LD ``ScholarlyArticle`` is preferred (it is the structured copy), then
    ``citation_*`` meta, then ``dc.*``. Every field is optional — :meth:`get` decides what a
    usable record needs.
    """
    text = page_html or ""
    pii = key[len("pii:"):] if key.lower().startswith("pii:") else ""
    fields = None
    for rec in _json_ld(text):
        candidate = _ld_fields(rec)
        if candidate["title"]:
            fields = candidate
            break
    if fields is None:
        fields = _meta_fields(_metas(text))
    if not fields["title"]:
        title_tag = _TITLE_TAG_RE.search(text)
        if title_tag:
            fields["title"] = _text(title_tag.group(1))
    creators = [normalize_author_name(a) for a in fields["creators"]]
    # A dc.identifier may be a doi.org URL; keep the bare DOI when it is one.
    doi = doi_from(fields["doi"]) or fields["doi"].strip()
    return AcademicItem(
        source="sciencedirect",
        key=f"pii:{pii}" if pii else key,
        title=fields["title"],
        creators=[c for c in creators if c["family"] or c["given"]],
        year=fields["year"],
        abstract=fields["abstract"],
        doi=doi,
        url=f"{_ARTICLE}/pii/{pii}" if pii else "",
        container_title=fields["container"],
        volume=fields["volume"],
        issue=fields["issue"],
        page=fields["page"],
        publisher=fields["publisher"],
        item_type="article",
        extra={"pii": pii} if pii else {},
    )


#: Hosts of the session cookies that are worth carrying (a browser cookie export lists others).
_COOKIE_HOSTS = ("elsevier", "sciencedirect")


def parse_cookie(raw: str) -> dict[str, str]:
    """Parse a cookie header / Netscape export, keeping only Elsevier/sciencedirect rows."""
    return _parse_cookie(raw, _COOKIE_HOSTS)


def load_cookie(cookie: str = "", cookie_file: str = "") -> str:
    """The ``Cookie`` header value from an inline string and/or a cookie file (both merged)."""
    return _load_cookie(cookie, cookie_file, _COOKIE_HOSTS, "sciencedirect")


class ScienceDirectBackend(EntitledBackend):
    """Resolve a DOI / PII / article URL to one ScienceDirect item and its PDF link.

    The site gates content behind a Cloudflare check plus an entitlement decision, neither of
    which a plain HTTP client can answer alone. A session you already entitled in your own
    browser is therefore carried in as a ``Cookie`` header (``[backends.sciencedirect]
    .cookie``/``.cookie_file``), and requests look like that browser's session.
    """

    UA = _UA
    headers: ClassVar[dict] = {
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    min_interval = 0.5
    rate = 2
    COOKIE_HOSTS = _COOKIE_HOSTS
    GATE_NAME = "the ScienceDirect clearance cookie"
    BLOCK_MSG = _BLOCK_MSG

    def __init__(self, name: str = "sciencedirect", timeout: float = 30.0,
                 cookie: str = "", cookie_file: str = "", user_agent: str = "", **_unused):
        super().__init__(name=name, timeout=timeout, cookie=cookie,
                         cookie_file=cookie_file, user_agent=user_agent)
        self._last_pii = self._last_page = ""

    # -- network ---------------------------------------------------------------

    def _pii_for_doi(self, doi: str) -> str:
        """Follow a DOI's redirect to its PII (Elsevier answers a ``linkinghub`` URL)."""
        url = f"https://doi.org/{urllib.parse.quote(doi, safe='/')}"
        # A 302 is the expected answer, so it must not be treated as an error.
        response = self.fetch(url, follow_redirects=False, raise_for_status=False)
        if response.status_code >= 400:
            raise for_status(response.status_code,
                             f"[{self.name}] HTTP {response.status_code} resolving DOI {doi}")
        location = response.headers.get("location", "")
        embedded = _URL_PII_RE.search(location or "")
        if not embedded:
            raise NotFound(f"[{self.name}] DOI does not resolve to a ScienceDirect article: {doi}")
        return _canonical_pii(embedded.group(1))

    def _pii(self, key: str) -> str:
        normalized = normalize_key(key)
        return self._pii_for_doi(normalized[4:]) if normalized.startswith("doi:") \
            else normalized[4:]

    def _page(self, pii: str) -> str:
        """Fetch the article page, turning a bot/entitlement gate into one clear error.

        The last page is kept: the fulltext chain calls ``get`` and then ``get_attachment``,
        which both need it, and the page does not change in between.
        """
        if self._last_page and self._last_pii == pii:
            return self._last_page
        url = f"{_ARTICLE}/pii/{urllib.parse.quote(pii)}"
        body = self._open(url).text
        self._last_pii, self._last_page = pii, body
        return body

    # -- Backend ---------------------------------------------------------------

    def search(self, query: str, limit: int = 10, filters: dict | None = None) -> list[AcademicItem]:
        """Resolve a DOI / PII / URL query to one item; keyword queries return [].

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
        """Fetch one article by DOI, PII or article URL and normalize it to ``pii:<ID>``.

        An entitled page also carries its ``pdfUrl``; it goes into ``attachments`` so the
        fulltext chain can take the item without a second page read.
        """
        pii = self._pii(key)
        body = self._page(pii)
        item = page_to_item(body, f"pii:{pii}")
        if not item.title and not item.doi:
            raise RuntimeError(f"[{self.name}] could not parse the article page: pii:{pii}")
        try:
            pdf_url = extract_pdf_url(body, pii)
        except RuntimeError:  # an odd host must not cost the metadata
            pdf_url = ""
        if pdf_url:
            item.attachments = [pdf_url]
        return item

    def get_attachment(self, key: str) -> bytes:
        """Download the article PDF (only an entitled session serves the link)."""
        pii = self._pii(key)
        url = extract_pdf_url(self._page(pii), pii)
        if not url:
            raise RuntimeError(f"[{self.name}] the article page carries no PDF link "
                               f"(not entitled, or the session needs an off-campus login)")
        try:
            data = self.fetch_bytes(url)
        except HttpError as e:
            if e.status in (302, 403) or is_block_page(e.body, e.status):
                raise Unavailable(f"[{self.name}] PDF download was gated: {_PDF_GATE_MSG}") from e
            raise
        if not data.startswith(b"%PDF"):
            raise Unavailable(f"[{self.name}] the PDF link did not return a PDF: {url} — "
                              f"{_PDF_GATE_MSG}")
        return data
