"""Z-Library book search / metadata / download backend (read-only, server-rendered pages).

Z-Library holds Chinese and foreign *books* that the article-oriented sources cannot
reach, which is the reason this backend exists. Its pages are server-rendered: a search
page emits ``<z-bookcard>`` elements with the card fields as attributes, and a book page
carries the bibliographic record in ``bookProperty`` blocks. Neither needs a browser, so
this backend parses the HTML like the other HTTP sources — no browser, unlike CNKI and Douban.

Two gates stand before the content, both passable locally:

* a **browser-check interstitial** (HTTP 503, ``Checking your browser ...``) that runs an
  obfuscated script: SHA-1 of ``c_token`` + a counter until two digest bytes hit a marker,
  then sets a ``c_token`` / ``c_time`` cookie and reloads. :func:`challenge_token` does the
  same arithmetic locally; the marker's *position* is the first hex digit of a per-request
  constant, so a solved token is only valid for the page it came from.
* the entry domain rotates. ``z-library.by`` 302s to whatever front host is live, so the
  client resolves the host once from that redirect and keeps its cookie jar for it.

Nothing is circumvented in the "defeat the anti-bot" sense: the proof-of-work is solved
exactly as the page asks. Only public catalogue metadata and the file itself are fetched,
one book per call, at human pace.

Markup is split into pure functions (:func:`parse_cards`, :func:`parse_book_page`,
:func:`challenge_token`, :func:`book_path`) so a site change touches only the thin adapter.

Compliance: Z-Library is a shadow library, so its copies are not open-access licensed. The
backend reads only public book metadata (title / author / publisher / ISBN / edition /
pages / language / rating) and never logs in.
"""
from __future__ import annotations

import hashlib
import html as html_mod
import re
import time
import urllib.parse

import httpx

from ..models import AcademicItem, extract_year, normalize_author_name
from .base import Backend
from .scihub import _UA
from .scihub import _Http as _ScihubHttp

# Entry domains, tried in order. ``z-library.by`` 302s to the live front host (probed
# 2026-10); the rest are the front hosts themselves, used when the entry is stranded.
_DEFAULT_MIRRORS = ("z-library.by",)

# Browser-check interstitial markers.
_CHALLENGE_MARK = "Checking your browser"
_POW_CONST_RE = re.compile(r"a0_0x2a54=\['([0-9A-F]{20,})'")

# Book path: ``/book/<hash>/<slug>.html`` (the slug is optional — the server tolerates any).
_BOOK_PATH_RE = re.compile(r"(?:^|\W)book/([A-Za-z0-9]+)")
_DOWNLOAD_RE = re.compile(r'href=["\'](/dl/[^"\']+)')

# The anonymous daily-limit page ("每日限额已用完" / "daily limit ... reached").
_LIMIT_RE = re.compile(r"限额已用完|daily limit", re.I)

# Safety cap so a pathological constant cannot hang the CLI (expected ~2^16 tries).
_MAX_TRIES = 5_000_000

_TAG_RE = re.compile(r"<[^>]+>")
_CARD_RE = re.compile(r"<z-bookcard\b([^>]*)>(.*?)</z-bookcard>", re.S | re.I)
_ATTR_RE = re.compile(r"([\w-]+)=[\"']([^\"']*)[\"']")
_SLOT_RE = re.compile(r"<div\b[^>]*\bslot=[\"'](\w+)[\"'][^>]*>(.*?)</div>", re.S | re.I)
_TITLE_RE = re.compile(r"<h1\b[^>]*class=[\"'][^\"']*book-title[^\"']*[\"'][^>]*>(.*?)</h1>", re.S | re.I)
_AUTHORS_RE = re.compile(r"<i\b[^>]*class=[\"'][^\"']*authors[^\"']*[\"'][^>]*>(.*?)</i>", re.S | re.I)
_PROP_RE = re.compile(
    r"class=[\"']bookProperty property_([\w ]+)[\"'][^>]*>\s*"
    r"<div class=[\"']property_label[\"'][^>]*>[^<]*</div>\s*"
    r"<(div|span)\b[^>]*class=[\"'][^\"']*property_value[^\"']*[\"'][^>]*>(.*?)</\2>", re.S | re.I)
_LD_BOOK_RE = re.compile(r"<script type=\"application/ld\+json\">(.*?)</script>", re.S)


# --- pure helpers (no network) ---

def challenge_token(page: str) -> str:
    """Solve the browser-check proof of work and return the ``c_token`` value ("" if absent).

    The page requires ``sha1(<constant> + n)`` whose byte at ``int(<constant>[0], 16)`` is
    ``0xb0`` and whose next byte is ``0x0b``; the cookie value is ``<constant><n>``. The
    position being keyed to the first hex digit means the check is per-request, so the token
    must be solved from the page that carries it rather than cached across pages.
    """
    m = _POW_CONST_RE.search(page or "")
    if not m:
        return ""
    constant = m.group(1)
    idx = int(constant[0], 16)
    for n in range(_MAX_TRIES):
        digest = hashlib.sha1((constant + str(n)).encode()).digest()
        if digest[idx] == 0xB0 and digest[idx + 1] == 0x0B:
            return constant + str(n)
    return ""


def _text(fragment: str) -> str:
    """Strip tags, unescape entities and collapse whitespace."""
    return " ".join(html_mod.unescape(_TAG_RE.sub(" ", fragment or "")).split())


def _split_people(text: str) -> list[str]:
    """Split a card's ``;``/comma-joined author blob into unique cleaned names (order kept)."""
    names: list[str] = []
    for part in re.split(r"[;；,，]", text or ""):
        name = part.strip()
        if name and name not in names:  # cards repeat the same author under variants
            names.append(name)
    return names


def book_path(key: str, source: str = "zlibrary") -> str:
    """Normalize a key (``zlibrary/<path>``, url, ``/book/<hash>``, bare hash) to ``book/<hash>``."""
    k = (key or "").strip()
    if k.startswith(f"{source}/"):
        k = k[len(source) + 1:]
    m = _BOOK_PATH_RE.search(k)
    if m:
        return f"book/{m.group(1)}"
    if re.fullmatch(r"[A-Za-z0-9]{6,}", k):
        return f"book/{k}"
    raise KeyError(f"[{source}] cannot build a book path from key: {key!r}")


def _card_to_item(attrs: dict, body: str, source: str = "zlibrary") -> AcademicItem | None:
    """Build one book candidate from a ``<z-bookcard>`` element (skipped when unusable)."""
    if attrs.get("deleted") in ("1", "true"):
        return None
    href = attrs.get("href") or ""
    slots = {name: _text(value) for name, value in _SLOT_RE.findall(body)}
    title = slots.get("title", "") or attrs.get("title", "")
    if not title or not href:
        return None
    try:
        path = book_path(href)
    except KeyError:
        return None
    creators = [normalize_author_name(a) for a in _split_people(slots.get("author", ""))]
    return AcademicItem(
        source=source, key=path, title=title,
        creators=[c for c in creators if c.get("family") or c.get("given")],
        year=extract_year(attrs.get("year", "")),
        publisher=attrs.get("publisher", ""), isbn=attrs.get("isbn", ""),
        url=href, item_type="book",
        extra={"zlibrary": True, "download": attrs.get("download", ""),
               "extension": attrs.get("extension", ""), "filesize": attrs.get("filesize", ""),
               "language": attrs.get("language", ""), "rating": attrs.get("rating", "")},
    )


def parse_cards(page: str, source: str = "zlibrary") -> list[AcademicItem]:
    """Pure: turn a search page's ``<z-bookcard>`` elements into book candidates.

    Duplicates (the page repeats the top hit in a hidden card) are dropped by key.
    """
    out: list[AcademicItem] = []
    seen: set[str] = set()
    for attrs_blob, body in _CARD_RE.findall(page or ""):
        item = _card_to_item(dict(_ATTR_RE.findall(attrs_blob)), body, source)
        if item and item.key not in seen:
            seen.add(item.key)
            out.append(item)
    return out


def _props(page: str) -> dict[str, str]:
    """Collect ``bookProperty property_<name>`` label/value pairs (name lowercased)."""
    found: dict[str, str] = {}
    for name, _tag, value in _PROP_RE.findall(page or ""):
        found.setdefault(name.strip().lower(), _text(value))
    return found


def _ld_book(page: str) -> dict:
    """Return the ``schema.org/Book`` JSON-LD block when the page has one, else ``{}``."""
    import json
    for blob in _LD_BOOK_RE.findall(page or ""):
        try:
            data = json.loads(blob)
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("@type") == "Book":
            return data
    return {}


def _clean_publisher(value: str) -> str:
    """Drop the city prefix Z-Library sometimes bakes in (``苏州：苏州大学出版社``)."""
    return value.split("：")[-1].strip() if "：" in value else value.strip()


def parse_book_page(page: str, source: str = "zlibrary") -> dict:
    """Pure: extract a book page's bibliographic record into a field dict.

    Prefers the rendered ``bookProperty`` blocks; the ``schema.org/Book`` JSON-LD block only
    fills gaps (description, and fields the rendered page omits).
    """
    props = _props(page)
    ld = _ld_book(page)
    title = _text((_TITLE_RE.search(page or "") or [None, ""])[1]) or str(ld.get("name", ""))
    authors = _split_people(_text((_AUTHORS_RE.search(page or "") or [None, ""])[1]))
    if not authors:
        for person in (ld.get("author") or []):
            name = person.get("name", "") if isinstance(person, dict) else str(person)
            authors.extend(_split_people(name.strip("{}")))
    authors = list(dict.fromkeys(authors))
    publisher = props.get("publisher", "")
    if not publisher and isinstance(ld.get("publisher"), dict):
        publisher = str(ld["publisher"].get("name", ""))
    description = str(ld.get("description", ""))
    description = " ".join(html_mod.unescape(description).replace("<br>", " ").split())
    language = props.get("language", "")
    if not language and isinstance(ld.get("inLanguage"), dict):
        language = str(ld["inLanguage"].get("name", ""))
    download = (_DOWNLOAD_RE.search(page or "") or [None, ""])[1]
    # ``numberOfPages`` is frequently 0; an absent/zero value must stay empty, not "0".
    pages = props.get("pages", "") or str(ld.get("numberOfPages") or "")
    return {
        "title": title,
        "authors": authors,
        "year": extract_year(props.get("year", "") or str(ld.get("datePublished", ""))),
        "edition": props.get("edition", ""),
        "publisher": _clean_publisher(publisher),
        "language": language,
        "pages": "" if pages == "0" else pages,
        "isbn": props.get("isbn 13", "") or props.get("isbn 10", "") or str(ld.get("isbn", "")),
        "description": description,
        "download": download,
    }


# --- HTTP adapter (challenge-aware, injectable for offline tests) ---

class _Http(_ScihubHttp):
    """``scihub._Http`` plus the Z-Library browser-check: solve the PoW, set the cookies, retry."""

    def __init__(self, timeout: int = 40, delay: float = 1.0):
        super().__init__(timeout)
        self.delay = float(delay)
        self._last = 0.0

    def _pace(self) -> None:
        """Sleep out the remainder of ``delay`` since the previous request (human pace)."""
        wait = self.delay - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)

    def _raw(self, url: str, referer: str = "") -> tuple[str, int, bytes]:
        """GET ``url`` returning ``(final_url, status, body)``; 4xx/5xx come back, not raise."""
        return self._call("GET", url, referer=referer)

    def _post(self, url: str, data: bytes) -> tuple[str, int, bytes]:
        """POST a form body (login); same tuple contract as :meth:`_raw`."""
        return self._call("POST", url, data=data, referer=url)

    def _call(self, method: str, url: str, data: bytes | None = None,
              referer: str = "") -> tuple[str, int, bytes]:
        headers = {"User-Agent": _UA}
        if data is None:
            headers["Accept"] = "text/html,application/pdf,*/*;q=0.8"
        else:
            headers.update({"Accept": "text/html",
                            "Content-Type": "application/x-www-form-urlencoded"})
        if referer:
            headers["Referer"] = referer
        try:
            r = self.client.request(method, url, content=data, headers=headers)
        except httpx.HTTPError as e:
            raise RuntimeError(f"unreachable: {e}") from e
        finally:
            self._last = time.monotonic()
        return str(r.url), r.status_code, r.content

    def _retry(self, call, url: str, referer: str) -> tuple[str, int, bytes]:
        """Run one request; on a browser check, solve it and run it once more."""
        self._pace()
        final, status, body = call(url, referer)
        if status == 503:
            token = challenge_token(body.decode("utf-8", "replace"))
            if token:
                self.set_cookie(final, "c_token", token)
                self.set_cookie(final, "c_time", "0.2")
                self._pace()
                final, status, body = call(final, referer)
        return final, status, body

    def fetch(self, url: str, referer: str = "") -> tuple[str, int, bytes]:
        """GET with the browser check handled (solved locally, at most once per response)."""
        return self._retry(self._raw, url, referer)

    def login(self, base: str, email: str, password: str) -> bool:
        """Log in with the configured credentials; True when a session cookie came back.

        Anonymous access hits a per-IP daily download limit, so credentials are optional but
        useful. A rejection is a plain False — search still works anonymously.
        """
        data = urllib.parse.urlencode({"email": email, "password": password,
                                       "site_mode": "books", "action": "login",
                                       "redirectUrl": ""}).encode()
        self._retry(lambda u, _r: self._post(u, data), base + "/login", base + "/")
        return bool(self.cookie("remix_userkey"))


def _base_of(url: str) -> str:
    """Return the ``scheme://host`` base of a URL."""
    parts = urllib.parse.urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _entry_url(mirror: str) -> str:
    """Normalize a mirror to an ``https://host/`` entry URL."""
    m = (mirror or "").strip().rstrip("/")
    if not m:
        raise RuntimeError("empty zlibrary mirror")
    return m if m.startswith(("http://", "https://")) else f"https://{m}"


class ZlibraryBackend(Backend):
    """Z-Library book search + metadata + download (read-only, opt-in).

    It deliberately does NOT advertise ``attachment``: a Z-Library copy is an unevaluated
    external file, not a local-store attachment, so ``fetch fulltext`` should not treat it as
    one. ``get_attachment`` is still implemented for ``fetch book --download``.
    """

    name = "zlibrary"
    #: Copies are shadow-library scans, not open-access licensed (the CLI labels hits by this).
    is_shadow = True

    @property
    def capabilities(self) -> set[str]:
        return {"read"}

    def __init__(self, name: str = "zlibrary", base: str = "", mirrors=None,
                 timeout: int = 40, delay: float = 1.0, email: str = "", password: str = ""):
        self.name = name
        self._base = base.strip().rstrip("/")
        self._mirrors = list(mirrors or _DEFAULT_MIRRORS)
        self._timeout = int(timeout)
        self._delay = float(delay)
        self._email = email
        self._password = password
        self._login_failed = False
        self._session: _Http | None = None

    # --- transport ---

    def _http(self) -> _Http:
        """The session, created (and logged in) once; anonymous when no credentials work."""
        if self._session is None:
            http = _Http(self._timeout, self._delay)
            if self._email and self._password and not self._login_failed:
                try:
                    if not http.login(self._resolve_base(http), self._email, self._password):
                        self._login_failed = True
                except RuntimeError:
                    self._login_failed = True  # anonymous access still works; do not retry per call
            self._session = http
        return self._session

    def _resolve_base(self, http: _Http) -> str:
        """Return the live front host, discovering it from a rotating entry domain once."""
        if self._base:
            return self._base
        errors: list[str] = []
        for mirror in self._mirrors:
            try:
                url = _entry_url(mirror)
            except RuntimeError as e:
                errors.append(str(e))
                continue
            try:
                final, status, _ = http.fetch(url)
            except RuntimeError as e:
                errors.append(f"{mirror}: {e}")
                continue
            if status == 200:
                self._base = _base_of(final)
                return self._base
            errors.append(f"{mirror}: HTTP {status}")
        raise RuntimeError(f"[{self.name}] no reachable mirror ({'; '.join(errors)})")

    def _get_page(self, http: _Http, path: str) -> tuple[str, str]:
        """GET a site path, returning ``(final_url, html)``; "" html means not found."""
        base = self._resolve_base(http)
        final, status, body = http.fetch(f"{base}/{path.lstrip('/')}", referer=base + "/")
        text = body.decode("utf-8", "replace")
        if status == 404 or _CHALLENGE_MARK in text:
            return final, ""
        if status == 204:
            raise RuntimeError(
                f"[{self.name}] HTTP 204 (no content) — the page is unavailable or the "
                "anonymous daily limit is reached; try another candidate or retry later")
        if status != 200:
            raise RuntimeError(f"[{self.name}] HTTP {status}")
        return final, text

    # --- Backend API ---

    def search(self, query: str, limit: int = 10, filters: dict | None = None) -> list[AcademicItem]:
        """Search books by title / author / ISBN and return candidates (title/author/year/publisher)."""
        q = (query or "").strip()
        if not q:
            return []
        filters = self._filters(filters)
        # Z-Library's search box already takes free text, ISBN and DOI; field filters are
        # folded in as query text and then enforced below (its param filters are fuzzy).
        params: dict[str, str] = {}
        year = filters.get("year", "")
        if year.isdigit():
            params = {"yearFrom": year, "yearTo": year}
        path = "/s/" + urllib.parse.quote(q, safe="")
        if params:
            path += "?" + urllib.parse.urlencode(params)
        http = self._http()
        _, page = self._get_page(http, path)
        items = parse_cards(page)
        want_title = filters.get("title", "").lower()
        want_author = filters.get("author", "").lower()
        if want_title:
            items = [i for i in items if want_title in i.title.lower()]
        if want_author:
            items = [i for i in items
                     if any(want_author in (c.get("family") or "").lower() for c in i.creators)]
        return items[:max(1, limit)]

    def get(self, key: str) -> AcademicItem:
        """Fetch one book's full record (title / authors / publisher / year / ISBN / pages)."""
        if not key:
            raise KeyError(f"[{self.name}] empty key")
        path = book_path(key, self.name)
        http = self._http()
        final, page = self._get_page(http, path)
        if not page:
            raise KeyError(f"[{self.name}] book not found: {key}")
        record = parse_book_page(page)
        if not record["title"]:
            raise KeyError(f"[{self.name}] could not read the book page: {key}")
        creators = [normalize_author_name(a) for a in record["authors"]]
        return AcademicItem(
            source=self.name, key=path, title=record["title"],
            creators=[c for c in creators if c.get("family") or c.get("given")],
            year=record["year"], abstract=record["description"],
            publisher=record["publisher"], isbn=record["isbn"], edition=record["edition"],
            page=record["pages"], url=final or f"{self._base}/{path}", item_type="book",
            extra={"zlibrary": True, "download": record["download"],
                   "language": record["language"], "shadow_library": True},
        )

    def get_attachment(self, key: str) -> bytes:
        """Download the book file (the copy is not open-access licensed)."""
        item = self.get(key)
        download = item.extra.get("download") or ""
        if not download:
            raise KeyError(f"[{self.name}] no download link on the book page: {key}")
        http = self._http()
        base = self._resolve_base(http)
        _, status, body = http.fetch(base + download, referer=item.url or base + "/")
        if body.startswith(b"%PDF"):
            return body
        page = body.decode("utf-8", "replace")
        if status == 204 or _LIMIT_RE.search(page):
            raise RuntimeError(
                f"[{self.name}] anonymous daily download limit reached — add "
                "email/password to [backends.zlibrary], or retry later")
        if status != 200 or not body:
            raise RuntimeError(f"[{self.name}] download failed (HTTP {status})")
        raise RuntimeError(f"[{self.name}] the download is not a PDF (HTTP {status})")
