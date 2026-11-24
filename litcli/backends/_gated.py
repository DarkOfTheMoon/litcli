"""The shared shape of the *guarded* publisher readers.

ACM, APS, IOP, Taylor & Francis, AIAA (Cloudflare managed challenge) and IEEE Xplore (AWS WAF
challenge) serve the article only to a session that got past the site's bot gate — and, beyond
it, only to a session the user entitled in their own browser. They share one body of logic:
the key shapes, the Highwire ``citation_*`` page parse, the PDF link pick and the OA marker.

What stays with a subclass is only what is site-specific: its host, its URL shapes, its PDF
path, its title suffix, and — where a site differs — a hook override. The page parse and the
PDF pick are module-level pure functions so a site module can expose them under its own name
(some are bound with the site's constants) and every branch stays testable offline.

The gate itself (``_open``, the memo, the refusal reason rebased to :class:`BotWall` /
:class:`Paywalled`) lives in :class:`~litcli.backends._entitled.EntitledBackend`; this layer
adds only the constant-driven page fetch, the item read and the PDF download.
"""
from __future__ import annotations

import html as html_mod
import re
import urllib.parse
from typing import ClassVar
from urllib.parse import urlparse

from .. import provenance
from ..errors import BadInput, NotFound, Paywalled
from ..models import AcademicItem, extract_year, normalize_author_name
from ..transport import HttpError
from ._cf_gate import gate_message, waf_gate_message
from ._entitled import EntitledBackend

#: A browser-ish default UA: a challenge is bound to the UA that cleared it.
_DEFAULT_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/131.0 Safari/537.36")

#: The cookie that actually carries the clearance, named in the construction error.
_GATE_NAMES = {
    "cf": "the Cloudflare clearance cookie (cf_clearance)",
    "waf": "the AWS WAF token cookie (aws-waf-token)",
}

_META_TAG_RE = re.compile(r"<meta\b[^>]*>", re.I)
_META_NAME_RE = re.compile(r"\bname=[\"']([^\"']+)[\"']", re.I)
#: The value is matched up to the opening quote's own kind: an apostrophe inside a title
#: (``Schrödinger's``) or a name (``O'Brien``) is content, not the closing delimiter.
_META_CONTENT_RE = re.compile(r"\bcontent=([\"'])(.*?)\1", re.I | re.S)
_TITLE_TAG_RE = re.compile(r"<title\b[^>]*>(.*?)</title>", re.S | re.I)
#: How a library-platform ``<title>`` separates its parts (a pipe, or a spaced dash).
_TITLE_SEP_RE = re.compile(r"\s*\|\s*|\s+[—–]\s+")
#: A subtitle segment (``Part II``, ``Volume 3``, a roman numeral) is content, not site chrome.
_SUBTITLE_RE = re.compile(
    r"^(?:part|volume|vol|chapter|section|appendix|supplement|no|number)\b"
    r"|^[IVXLCDM]{1,6}$", re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

#: A DOI after any of the accepted prefixes (``doi:``, a ``doi.org`` URL, the site's own
#: ``name:``/``name/`` form), which may stack.
_DOI_PREFIX_RE = re.compile(
    r"^(?:(?:https?://(?:dx\.)?doi\.org/|doi:\s*|[a-z]+[:\/]\s*)\s*)+", re.I)
_DOI_RE = re.compile(r"^10\.\d{4,9}/[^\s?#]+$")
#: A DOI in running text (a ``doi.org`` hop leaves the host before it).
_DOI_FIND_RE = re.compile(r"10\.\d{4,9}/[^\s?#]+")
#: Characters a DOI picks up from running text / a URL tail but does not own.
_DOI_TAIL = ".,;:)]}/"

#: Every marker of an open-access item: the licence wording, or the site's own badge class
#: (IEEE/IOP stamp ``open-access`` on the *document's* badge only; their nav says "Open
#: Access" on every page but carries no such class). The bare ``creative-commons__description``
#: *class* is not enough on its own: related-item cards on the same page carry that class too,
#: so the block must also say what it is. A parameterised set keeps the marker in one place.
OA_MARKERS = ("is an open access article",
              r"creative-commons__description[^>]*>\s*this (?:is|article)",
              "distributed under the terms of the creative commons",
              r"""class=["'][^"']*\bopen-access""")
OA_RE = re.compile("|".join(OA_MARKERS), re.I)


def _unescaped_when_safe(url: str) -> str:
    return html_mod.unescape((url or "").replace("\\/", "/")).strip()


def _mentions(text: str, needle: str) -> bool:
    """True when ``needle`` occurs in ``text`` as a whole token.

    Used to tell this item's own PDF anchor from a related-item card's: both carry a DOI or an
    internal id, and only the exact one matches. A dot *and more digits* after the match also
    counts as "inside" a longer DOI (``10.1145/1`` must not match ``10.1145/1.2``), while a
    dot before a suffix is a real boundary (``…/10.1063/5.0123456.pdf``).
    """
    start = text.find(needle)
    while start != -1:
        before = text[start - 1] if start else ""
        end = start + len(needle)
        after = text[end] if end < len(text) else ""
        longer = after == "." and text[end + 1:end + 2].isdigit()
        if not before.isalnum() and not after.isalnum() and not longer:
            return True
        start = text.find(needle, start + 1)
    return False


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
            found.setdefault(key, []).append(content.group(2))
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
                                 "citation_inbook_title", "citation_book_title", "dc.source")),
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


# --- key normalization (a pure function; the site passes its own shapes) ---

def _clean_doi(value: str) -> str:
    """Decode a DOI, cut a query/fragment off it and drop the separators text trails along."""
    return urllib.parse.unquote(
        re.split(r"[?#\s]", value or "", maxsplit=1)[0]).rstrip(_DOI_TAIL)


def doi_from(key: str) -> str:
    """Return a bare DOI from a DOI-shaped key (a ``doi.org``/``site:`` form included).

    A plain page URL is searched too: the prefix strip leaves ``//host/...`` behind, which is
    not DOI-shaped, yet the path may well carry the DOI the page names. Either way a query
    tail (``?download=true``), a fragment and a trailing separator are cut off: ``doi.org``
    would otherwise be handed ``xyz.123%3Fdownload=true``.
    """
    k = _clean_doi(_DOI_PREFIX_RE.sub("", (key or "").strip()).strip())
    if _DOI_RE.match(k):
        return k
    found = _DOI_FIND_RE.search(key or "")
    return _clean_doi(found.group(0)) if found else ""


def normalize_key(key: str, *, tag: str, url_re: re.Pattern, forms: str,
                  embedded_doi_re: re.Pattern | None = None) -> str:
    """Classify a key and return ``doi:<DOI>`` or ``url:<item URL>``.

    A pasted URL may embed the DOI (``/doi/<DOI>``) — the DOI is then the better key — or
    percent-encode it, which the DOI shapes do not match; a failing key is retried decoded.
    """
    k = (key or "").strip()
    if k.lower().startswith("url:"):  # an already-normalized key keeps its kind
        k = k[len("url:"):].strip()
    k = re.sub(rf"^(?:{tag}[:\/]\s*)+", "", k, flags=re.I).strip()
    if not k:
        raise BadInput(f"[{tag}] empty key (expected {forms})")
    for candidate in ((k, urllib.parse.unquote(k)) if "%" in k else (k,)):
        if url_re.match(candidate):
            # The URL carries the DOI in its path: that is the better key. The path may
            # percent-encode the DOI slash (``10.1002%2Fa%2Fb``), which the DOI shape does not
            # match, so the embedded extraction is retried decoded — the ``url:`` fallback
            # below would keep the encoding.
            embedded = embedded_doi_re.search(candidate) if embedded_doi_re else None
            if not embedded and ("?" in candidate or "#" in candidate):
                # Some site shapes anchor the DOI at the URL end; a query/fragment tail would
                # hide it. Search the path alone.
                embedded = (embedded_doi_re.search(re.split(r"[?#]", candidate, maxsplit=1)[0])
                            if embedded_doi_re else None)
            if not embedded and "%" in candidate:
                embedded = (embedded_doi_re.search(urllib.parse.unquote(candidate))
                            if embedded_doi_re else None)
            if embedded:
                return f"doi:{urllib.parse.unquote(embedded.group(1)).rstrip('/.,;:')}"
            return f"url:{candidate.split('#', 1)[0]}"
        doi = doi_from(candidate)
        if doi:
            return f"doi:{doi.rstrip('/.,;:')}"
    raise BadInput(f"[{tag}] not a DOI, {forms}: {key!r}")


# --- page parse (pure) ---

def page_to_item(page_html: str, key: str, url: str = "", *, source: str, title_suffix: str,
                 item_type=None) -> AcademicItem:
    """Normalize an item page to an :class:`AcademicItem` (``citation_*`` → ``dc.*`` → title).

    ``key`` is the normalized key; the page's own ``citation_doi`` is the truth for ``doi``
    (a ``url:`` key names only an internal id). ``title_suffix`` is stripped off a title-tag
    fallback, and ``item_type`` turns the landed URL into a type where the site distinguishes
    one (a book chapter, say).
    """
    page = page_html or ""
    fields = _fields(_metas(page))
    if not fields["title"]:
        title_tag = _TITLE_TAG_RE.search(page)
        if title_tag:
            raw_title = title = _text(title_tag.group(1))
            if title_suffix:
                # A library-platform title tag is ``<title> | <journal> | <site>``: drop the
                # site and journal chrome, but keep a subtitle segment (``| Part II``) — that
                # one is content, and a greedy tail strip used to eat it along with the rest.
                segments = [s.strip() for s in _TITLE_SEP_RE.split(raw_title) if s.strip()]
                if segments and segments[-1].lower() == title_suffix.lower():
                    segments.pop()
                while len(segments) > 1 and not _SUBTITLE_RE.match(segments[-1]) \
                        and segments[-1].lower() != title_suffix.lower():
                    segments.pop()
                title = " | ".join(segments).strip()
            fields["title"] = title
    doi = fields["doi"] or doi_from(key.split(":", 1)[-1])
    creators = [normalize_author_name(a) for a in fields["creators"]]
    return AcademicItem(
        source=source,
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
        item_type=item_type(url, key) if item_type else "article",
    )


class GatedBackend(EntitledBackend):
    """Base for a reader on a bot-gated site the user clears in their own browser.

    The clearance cookie is bound to the browser's ``user_agent`` and its egress IP, so the two
    travel together and a missing one is a construction error. Every page request goes through
    :meth:`_open`, which turns a challenge into :class:`BotWall`; the PDF download judges the
    same way (:class:`Unavailable`) and reports an entitled-but-refused answer as
    :class:`Paywalled`.
    """

    #: The cookie name in the construction error (derived from GATE_KIND if blank).
    GATE_NAME = ""
    #: The publisher's name in the gate message.
    SITE_NAME = ""
    #: The site base URL (for a redirect/check message and the PDF rewrite).
    SITE = ""
    #: Hosts the cookie export may carry (defaults to ``ALLOWED_HOSTS`` when left empty).
    COOKIE_HOSTS: ClassVar[tuple[str, ...]] = ()
    #: An item URL on this site; a DOI embedded in a ``/doi/`` path; an anchor path that is a PDF.
    URL_RE: ClassVar[re.Pattern | None] = None
    EMBEDDED_DOI_RE: ClassVar[re.Pattern | None] = None
    PDF_PATH_RE: ClassVar[re.Pattern | None] = None
    #: The last-resort PDF URL (``{site}``/``{doi}`` filled in).
    PDF_URL_TEMPLATE = ""
    #: The item id in a landed URL, for correlating the PDF anchor with this item.
    ITEM_ID_RE: ClassVar[re.Pattern | None] = None
    #: The open-access licence block, which only open-access pages carry.
    OA_RE: ClassVar[re.Pattern] = OA_RE
    #: The documented key forms, spelled into the bad-key message.
    KEY_FORMS = "a DOI or an article URL"
    #: The ``source``/prefix name (``acm:``, ``acm/``, ``doi:``).
    KEY_PREFIX = ""
    #: What the site calls itself in a page-title suffix.
    TITLE_SUFFIX = ""
    #: True where the page names its PDF link even without a session (SpringerLink, Cambridge).
    ANONYMOUS_PAGES = False
    #: The default backend name (the registry passes it, a bare construction does not).
    DEFAULT_NAME = ""
    UA = _DEFAULT_UA
    headers: ClassVar[dict] = {
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    #: Hold a page-per-second slot; ``rate`` allows a four-request burst from the limiter.
    min_interval = 1.0
    rate = 4

    def __init__(self, name: str = "", timeout: float | None = None,
                 delay: float | None = None, cookie: str = "", cookie_file: str = "",
                 user_agent: str = "", **_unused):
        name = name or self.DEFAULT_NAME
        self.GATE_NAME = self.GATE_NAME or _GATE_NAMES.get(self.GATE_KIND, "") \
            or "the session cookie"
        if not self.BLOCK_MSG:
            self.BLOCK_MSG = self._gate_message(name)
        super().__init__(name=name, timeout=timeout, delay=delay, cookie=cookie,
                         cookie_file=cookie_file, user_agent=user_agent)

    def _gate_message(self, name: str) -> str:
        if self.GATE_KIND == "waf":
            return waf_gate_message(self.SITE_NAME, name)
        return gate_message(self.SITE_NAME, name)

    # -- pure-function bindings ------------------------------------------------

    def _on_site(self, url: str) -> bool:
        return self.matches_url(url)

    def normalize_key(self, key: str) -> str:
        return normalize_key(key, tag=self.KEY_PREFIX, url_re=self.URL_RE, forms=self.KEY_FORMS,
                             embedded_doi_re=self.EMBEDDED_DOI_RE)

    def item_id(self, url: str) -> str:
        # The whole URL, not just its path: a site may carry the id in the query.
        match = self.ITEM_ID_RE.search(url or "") if self.ITEM_ID_RE else None
        return match.group(1) if match else ""

    def is_open_access(self, page_html: str) -> bool:
        """Overrides the base default with the site's own licence-block marker."""
        return bool(self.OA_RE.search(page_html or ""))

    def extract_pdf_url(self, page_html: str, doi: str = "", want_id: str = "") -> str:
        return extract_pdf_url(page_html, doi=doi, want_id=want_id, site=self.SITE,
                               pdf_path_re=self.PDF_PATH_RE, template=self.PDF_URL_TEMPLATE,
                               on_site=self._on_site, tag=self.KEY_PREFIX)

    def item_type(self, url: str, key: str = "") -> str:
        """The item type for a landed URL (a site that has more than articles overrides it)."""
        return "article"

    def page_to_item(self, page_html: str, key: str, url: str = "") -> AcademicItem:
        return page_to_item(page_html, key, url, source=self.name, title_suffix=self.TITLE_SUFFIX,
                            item_type=lambda u, k: self.item_type(u, k))

    # -- network ---------------------------------------------------------------

    def _page(self, key: str) -> tuple[str, str]:
        """Fetch the item page as ``(html, landed url)``, turning a gate into one clear error.

        The page is memoized per key: the PDF download and the item read share one request. A
        DOI goes through ``doi.org``; the landed URL is kept because the item id in it is not
        derivable from the DOI.
        """
        normalized = self.normalize_key(key)
        hit = self._memo_get(normalized)
        if hit is not None:
            return hit
        if normalized.startswith("url:"):
            url = normalized[len("url:"):]
        else:
            url = f"https://doi.org/{urllib.parse.quote(normalized[len('doi:'):], safe='/')}"
        response = self._open(url)  # a gate is turned into BotWall; 404 propagates
        landed = str(response.url) or url
        if not self._on_site(landed):
            raise NotFound(f"[{self.name}] DOI does not resolve to an {self.SITE_NAME} item: "
                           f"{normalized}")
        self._memo_put(normalized, (response.text, landed))
        return self._memo_get(normalized)

    # -- Backend ---------------------------------------------------------------

    def get(self, key: str) -> AcademicItem:
        """Fetch one item by DOI or URL and, when the page names one, its PDF link.

        A 404 is turned into :class:`NotFound` so a search treating the key as a candidate
        yields nothing instead of an error.
        """
        normalized = self.normalize_key(key)
        try:
            body, landed = self._page(normalized)
        except HttpError as e:
            if e.status in (404, 410):
                raise NotFound(f"[{self.name}] no such item: {key}") from e
            raise
        item = self.page_to_item(body, normalized, landed)
        if normalized.startswith("url:") and item.doi:
            # The URL named only an internal id; the page named the real identity, which also
            # becomes the memo key so the PDF download reuses this page.
            item.key = f"doi:{item.doi}"
            self._memo_put(f"doi:{item.doi}", (body, landed))
        if not item.title:
            raise RuntimeError(f"[{self.name}] could not parse the item page: {landed}")
        self._guard_sign_in(body, landed, item.title)
        try:
            pdf_url = self.extract_pdf_url(body, item.doi, self.item_id(landed))
        except RuntimeError:  # an odd host must not cost the metadata
            pdf_url = ""
        if pdf_url:
            item.attachments = [pdf_url]
        if self.is_open_access(body) and not self._short_page(landed):
            # The chain labels the hit from this: an open-access item is not a subscription
            # copy, so its note and its "needs a session" advice would both be wrong. A licence
            # block on the paywalled *short* page can be a related-item card's, hence the check.
            item.extra = {**item.extra, "open_access": True}
            item.access = provenance.OA
        return item

    def get_attachment(self, key: str) -> bytes:
        """Download the item PDF (only a session the site lets through serves it)."""
        normalized = self.normalize_key(key)
        page, landed = self._page(normalized)
        url = self.extract_pdf_url(page, doi_from(normalized), self.item_id(landed))
        if not url:
            raise Paywalled(f"[{self.name}] the item page carries no PDF link: {landed}")
        return self._gated_download(url, page, landed)


def extract_pdf_url(page_html: str, doi: str = "", want_id: str = "", *, site: str,
                    pdf_path_re: re.Pattern | None, template: str, on_site, tag: str) -> str:
    """Return the item's PDF link, or "" when the page carries none.

    The page's own ``citation_pdf_url`` meta is authoritative; older markup carries a plain
    anchor instead, and related-item cards on the same page carry the same shape — so an anchor
    only counts when its path names this item's id (``want_id``) or its DOI (``doi``). ``doi``
    is also the last-resort rewrite through ``template``. The result must stay on the site — a
    third party here means the page handed back something unexpected; raising keeps that visible.
    """
    text = page_html or ""
    # The item's own identity: its internal id where the site has one, else its DOI (which
    # these anchors embed verbatim). A related-item card on the same page differs in it.
    needles = [n for n in (want_id, doi) if n]
    raw = _unescaped_when_safe((_metas(text).get("citation_pdf_url") or [""])[0])
    if not raw and pdf_path_re:
        first = ""
        for candidate in pdf_path_re.findall(text):
            first = first or candidate
            if any(_mentions(candidate, n) for n in needles):
                raw = candidate
                break
        raw = _unescaped_when_safe(raw or first)
    if not raw and template:
        if "{id}" in template and want_id:
            raw = template.format(site=site, doi="", id=want_id)
        elif doi and "{doi}" in template:
            raw = template.format(site=site, doi=urllib.parse.quote(doi, safe="/"), id=want_id)
    if not raw:
        return ""
    # Cut back to the canonical path unless the regex only stops short of the *query tail*: an
    # endpoint like Xplore's stampPDF needs ``tag``/``isnumber`` beside ``arnumber``, while
    # Cambridge's ``/div-class-title-…-div.pdf`` tail after the ``.pdf`` adds nothing.
    match = pdf_path_re.search(raw) if pdf_path_re else None
    if match and not raw[match.end():].startswith(("?", "&")):
        raw = match.group(1)
    if raw.startswith("//"):
        raw = "https:" + raw
    elif raw.startswith("/"):
        raw = site + raw
    if not on_site(raw):
        host = urlparse(raw).hostname or raw
        raise RuntimeError(f"[{tag}] PDF link is not on the site: {host}")
    return raw


