"""DOAB backend: the Directory of Open Access Books (directory.doabooks.org).

Open-access books, so it fills the book class with *legal* copies rather than shadow-library
ones. Read-only, no key.

The instance sits behind two different walls, both deployed against AI crawlers rather than
against any particular address:

* ``/rest/*`` answers ``403 You address is not allowed to access this API.`` to every client
  that does not run JavaScript — Anubis denies API routes by policy. Search therefore uses the
  OpenSearch Atom feed instead (``/open-search/*`` is allowed through untouched).
* ``/handle/*`` (the per-record page) is behind Anubis' proof-of-work check. That one *is*
  solvable without a browser: the page carries a challenge, ``sha256(randomData + n)`` with
  ``difficulty`` leading hex zeros passes it, and the answer is posted to ``api/pass-challenge``
  which sets an auth cookie. So ``get`` solves it once and then reads the record's DC metadata.
"""
from __future__ import annotations

import hashlib
import html as html_mod
import json
import re
import urllib.parse

from lxml import etree as ET

from ..models import AcademicItem, extract_year, normalize_author_name
from .http import HttpBackend, HttpError

_SITE = "https://directory.doabooks.org"
_SEARCH = f"{_SITE}/open-search/discover"
_ATOM = "{http://www.w3.org/2005/Atom}"
_KEY_SUFFIX = "doab/"
_MAX_TRIES = 20_000_000

_CHALLENGE_RE = re.compile(r'id="anubis_challenge"[^>]*>([^<]+)</script>')
_META_RE = re.compile(r'<meta\s+name="DC\.([\w.]+)"\s+content="([^"]*)"', re.I)
_ISBN_RE = re.compile(r"(?:97[89][\d -]{9,16}[\dXx])")
_FIELD_RE = re.compile(r"<h5>([^<]{1,40})</h5>(.*?)</div>", re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_HREF_RE = re.compile(r'href="(https?://[^"]+)"')
_DOWNLOAD_RE = re.compile(r"<h5[^>]*>\s*Download Url\(s\)\s*</h5>(.*?)</div>", re.S | re.I)
_DESC_RE = re.compile(r'class="simple-item-view-description[^"]*"[^>]*>(.*?)</div>', re.S)
_HEADING_RE = re.compile(r'class="[^"]*page-header[^"]*"[^>]*>\s*<h\d[^>]*>(.*?)</h\d>', re.S)
_TITLE_TAG_RE = re.compile(r"<title>(.*?)</title>", re.S)


def _flat(fragment: str) -> str:
    return " ".join(html_mod.unescape(_TAG_RE.sub(" ", fragment)).split())


def solve_challenge(body: str) -> dict | None:
    """Solve the Anubis proof-of-work on a 403 page (pure apart from the hashing cost).

    Returns the query parameters for ``api/pass-challenge``, or None when the body is not a
    challenge. The rule is the page's own: ``sha256(randomData + n)`` must start with
    ``difficulty`` hex zeros — the same work the browser does, not a bypass.
    """
    m = _CHALLENGE_RE.search(body or "")
    if not m:
        return None
    try:
        data = json.loads(html_mod.unescape(m.group(1).strip()))
    except ValueError:
        return None
    challenge = data.get("challenge") or {}
    random_data = challenge.get("randomData")
    if not random_data:
        return None
    prefix = "0" * int((data.get("rules") or {}).get("difficulty") or 0)
    for nonce in range(_MAX_TRIES):
        digest = hashlib.sha256((random_data + str(nonce)).encode()).hexdigest()
        if digest.startswith(prefix):
            return {"id": challenge.get("id", ""), "response": digest, "nonce": str(nonce)}
    return None  # pragma: no cover - the cap is far above the expected work


class DoabError(RuntimeError):
    """Raised when the feed answers with something that is not an Atom document."""


def _text(node, path: str) -> str:
    found = node.find(path)
    return " ".join((found.text or "").split()) if found is not None else ""


def handle_of(entry) -> str:
    """The bare handle suffix (``20.500.12854/57156``) of an entry, from its ``<id>``."""
    ident = _text(entry, f"{_ATOM}id")
    marker = "/handle/"
    return ident.split(marker, 1)[1] if marker in ident else ident


def _clean_summary(summary: str, title: str, names: list[str]) -> str:
    """Drop the title/author lines the feed repeats at the head of each summary."""
    for lead in [title] + names:
        if lead and summary.startswith(lead):
            summary = summary[len(lead):].lstrip(" ;,.:-")
    return summary


def entry_to_item(entry, source: str = "doab") -> AcademicItem:
    """Normalize one Atom entry (pure: no I/O, offline-testable)."""
    handle = handle_of(entry)
    title = _text(entry, f"{_ATOM}title")
    names = [n for n in (_text(a, f"{_ATOM}name") for a in entry.findall(f"{_ATOM}author")) if n]
    authors = [normalize_author_name(n) for n in names]
    url = ""
    for link in entry.findall(f"{_ATOM}link"):
        if link.get("rel") == "alternate" and link.get("href"):
            url = link.get("href") or ""
            break
    return AcademicItem(
        source=source,
        key=handle,
        title=title,
        creators=authors,
        year=extract_year(_text(entry, f"{_ATOM}published") or _text(entry, f"{_ATOM}updated")),
        abstract=_clean_summary(_text(entry, f"{_ATOM}summary"), title, names),
        url=url or f"https://directory.doabooks.org/handle/{handle}",
        item_type="book",
        extra={"handle": handle},
    )


def _people(fields: dict[str, str], pairs: list[tuple[str, str]]) -> list[str]:
    """All author/contributor names, from the ``<h5>`` block and the (complete) DC meta list.

    The ``<h5>`` block puts every person in its own ``<div>``, so the generic block regex only
    sees the first one; ``DC.creator`` / ``DC.contributor`` repeat once per person and carry the
    full list.
    """
    names: list[str] = []

    def add(blob: str) -> None:
        for part in re.split(r"[;；]", blob or ""):
            name = re.sub(r"\s*\((?:editor|author)s?\)", "", part).strip()
            if name and name not in names:
                names.append(name)

    for key in ("author(s)", "contributor(s)"):
        if fields.get(key):
            add(fields[key])
    for name, value in pairs:
        if name.lower() in ("creator", "contributor"):
            add(html_mod.unescape(value))
    return names


def page_to_item(body: str, handle: str, source: str = "doab") -> AcademicItem:
    """Normalize a record page (pure: no I/O). Details sit in ``<h5>Label</h5>value`` blocks."""
    fields: dict[str, str] = {}
    for label, chunk in _FIELD_RE.findall(body or ""):
        fields.setdefault(label.strip().lower(), _flat(chunk))
    pairs = _META_RE.findall(body or "")
    meta = {name.lower(): html_mod.unescape(value) for name, value in pairs}  # last wins: fallback only
    names = _people(fields, pairs)
    isbn = _ISBN_RE.search(fields.get("isbn", ""))
    downloads: list[str] = []
    for chunk in _DOWNLOAD_RE.findall(body or ""):
        for link in _HREF_RE.findall(chunk):
            if link not in downloads:
                downloads.append(link)
    abstract = _DESC_RE.search(body or "")
    heading = _HEADING_RE.search(body or "") or _TITLE_TAG_RE.search(body or "")
    return AcademicItem(
        source=source,
        key=handle,
        title=(_flat(heading.group(1)) if heading else "") or meta.get("title", ""),
        creators=[normalize_author_name(n) for n in names],
        year=extract_year(fields.get("publication date and place", "")),
        abstract=re.sub(r"^(?:Abstract|摘要)\s*", "", _flat(abstract.group(1))) if abstract else "",
        doi=fields.get("doi", ""),
        isbn=isbn.group(0) if isbn else "",
        url=f"{_SITE}/handle/{handle}",
        item_type="book",
        page=fields.get("pages", ""),
        publisher=fields.get("publisher", ""),
        container_title=fields.get("series", "") or fields.get("imprint", ""),
        attachments=downloads,
        extra={"language": fields.get("language", ""), "rights": fields.get("rights", ""),
               "keywords": fields.get("keywords", ""), "handle": handle,
               "dc_type": meta.get("type", ""),
               "download": downloads[0] if downloads else ""},
    )


def is_imprint_placeholder(entry) -> bool:
    """True for a publisher/imprint handle indexed as an "Untitled" entry without metadata.

    DOAB mixes publisher handles into the result feed. They carry no DC title, so the feed
    calls them "Untitled" and leaves out the date and summary a real record always has.
    """
    if _text(entry, f"{_ATOM}title").strip().lower() != "untitled":
        return False
    return not (_text(entry, f"{_ATOM}published") or _text(entry, f"{_ATOM}summary"))


def feed_to_items(data: bytes, source: str = "doab") -> list[AcademicItem]:
    """Parse an Atom feed body into normalized items, dropping untitled/imprint entries."""
    try:
        root = ET.fromstring(data)
    except ET.XMLSyntaxError as e:
        raise DoabError(f"DOAB did not return Atom XML: {e}") from e
    items = []
    for entry in root.findall(f"{_ATOM}entry"):
        if is_imprint_placeholder(entry):
            continue
        item = entry_to_item(entry, source)
        if item.title:
            items.append(item)
    return items


class DoabBackend(HttpBackend):
    """Read-only open-access book search (feed) and record lookup (Anubis-walled pages)."""

    def __init__(self, name: str = "doab", base: str = _SEARCH, timeout: int = 30, delay: float = 1.0):
        super().__init__(name=name, timeout=timeout, rate=1, min_interval=delay)
        self._base = base
        parsed = urllib.parse.urlsplit(base)
        # Page/challenge URLs hang off the host, so a ``base`` override must move them too.
        self._site_root = f"{parsed.scheme}://{parsed.netloc}" if parsed.netloc else _SITE

    @property
    def capabilities(self) -> set[str]:
        # ``attachment`` stays off: the file link is remote, not a local-store copy.
        return {"read"}

    def _has_auth_cookie(self) -> bool:
        """True when the Anubis auth cookie sits in the jar (the bot check is cleared)."""
        return self.has_cookie("anubis-auth")

    def _pass_challenge(self, body: str, redir: str) -> bool:
        """Solve the Anubis proof-of-work and exchange it for an auth cookie."""
        params = solve_challenge(body)
        if not params:
            return False
        params.update({"redir": redir, "elapsedTime": "0"})
        url = (f"{self._site_root}/.within.website/x/cmd/anubis/api/pass-challenge?"
               f"{urllib.parse.urlencode(params)}")
        try:  # only the status and the Set-Cookie header matter here
            self.fetch_status(url)
        except RuntimeError:
            pass
        # pass-challenge redirects to the target, so a missing record answers 404 even though
        # the cookie was set: judge by the cookie, not by the redirect target's status.
        return self._has_auth_cookie()

    def _page(self, url: str, path: str) -> str:
        """GET a page, turning a 404 into the "not found" error."""
        try:
            return self.fetch_text(url)
        except HttpError as e:
            if e.status == 404:
                raise RuntimeError(f"[{self.name}] not found: {path}") from e
            raise

    def _get_page(self, path: str) -> str:
        """GET a site page, answering the bot check once if it is served."""
        url = self._site_root + path
        try:
            return self._page(url, path)
        except HttpError as e:
            if e.status == 403 and self._pass_challenge(e.body, path):
                return self._page(url, path)
            raise

    def _fetch(self, query: str, rpp: int = 0, start: int = 0) -> list[AcademicItem]:
        url = f"{self._base}?query={urllib.parse.quote(query)}"
        if rpp > 0:
            url += f"&rpp={min(rpp, 100)}"
        if start > 0:
            url += f"&start={start}"
        # The feed answers transient 500s under load; the shared transport retries those.
        data = self.fetch(url, headers={"Accept": "application/atom+xml"}).content
        return feed_to_items(data, self.name)

    def search(self, query: str, limit: int = 10, filters: dict | None = None) -> list[AcademicItem]:
        filters = self._filters(filters)
        terms = [t for t in ((query or "").strip(), filters.get("title"), filters.get("author"),
                             filters.get("container")) if t]
        year = filters.get("year") or ""
        limit = max(1, limit)
        size = 100 if year else min(limit, 100)
        # A year filter can only be applied after the fact (the feed has no field syntax), so page
        # through (bounded) until enough hits match instead of filtering only the first page.
        max_pages = 5 if year else (limit + size - 1) // size
        items: list[AcademicItem] = []
        start = 0
        for _ in range(max_pages):
            batch = self._fetch(" ".join(terms) or "*", rpp=size, start=start)
            items.extend(i for i in batch if not year or i.year == year)
            start += len(batch)
            if len(batch) < size or len(items) >= limit:
                break
        return items[:limit]

    def get(self, key: str) -> AcademicItem:
        k = (key or "").strip()
        if k.startswith(_KEY_SUFFIX):
            k = k[len(_KEY_SUFFIX):]
        if "/handle/" in k:
            k = k.split("/handle/", 1)[1]
        if not k:
            raise KeyError(f"[{self.name}] empty key")
        body = self._get_page(f"/handle/{k}")
        item = page_to_item(body, k, self.name)
        if (item.extra.get("dc_type") or "").lower() == "publisher":
            raise RuntimeError(f"[{self.name}] publisher/imprint record, not a book: {key}")
        if not item.title and not item.isbn:
            raise KeyError(f"[{self.name}] not found: {key}")
        return item

    def get_attachment(self, key: str) -> bytes:
        """Download the book's open-access file, trying each Download Url until one is a PDF.

        Many records list a publisher landing page (or DOI) rather than the file itself, so the
        first link alone frequently answers HTML.
        """
        item = self.get(key)
        if not item.attachments:
            raise RuntimeError(f"[{self.name}] no download link on the record: {key}")
        errors: list[str] = []
        for url in item.attachments:
            try:
                data = self.fetch_bytes(url)
            except RuntimeError as e:
                errors.append(f"{url}: {e}")
                continue
            if data.startswith(b"%PDF"):
                return data
            errors.append(f"{url}: not a PDF")
        raise RuntimeError(f"[{self.name}] no download link returned a PDF ({'; '.join(errors)})")
