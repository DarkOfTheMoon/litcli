"""Douban (豆瓣) Chinese book metadata search backend (read-only, Playwright).

Douban has no public API (``api.douban.com/v2/book/search`` returns HTTP 400 without an
app key that individuals cannot obtain), and its book pages are JavaScript-rendered. So,
like CNKI, this backend drives a Chromium via Playwright. Unlike CNKI it needs no slider
captcha and no logged-in session — the search page's SSR payload (``window.__DATA__``)
already carries the book card JSON — so it runs headless by default.

Extraction is split in two, as in CNKI:

* a *pure* layer: :func:`window_data_to_items` (search cards) and the info-string parser
  plus :func:`_subject_to_item` (single subject page) turn plain dicts into
  ``AcademicItem`` objects. They never touch a browser, so they are unit-testable offline
  and survive Douban markup changes (only the adapter below changes).

* a thin Playwright *adapter* (:class:`DoubanBackend`) that opens the search / subject
  pages, reads the rendered ``window.__DATA__`` or ``#info``, and feeds the pure parsers.

Compliance: this reads only *public* bibliographic metadata (title / author / publisher /
ISBN / edition-year / pages / rating). It does not scrape anything gated behind a login,
does not bypass anti-bot checks, and runs at a low human-scale frequency. If Douban rate-
limits (a "检测到异常流量" wall), it surfaces a clear error rather than circumventing it.
"""
from __future__ import annotations

import json
import re
from urllib.parse import urlencode

from ..models import AcademicItem, extract_year, normalize_author_name
from ..paths import config_dir
from .playwright import _PLAYWRIGHT_HINT, PlaywrightBackend

# Product version, used only by the pure parsers for the AcademicItem.extra provenance.
_SOURCE = "douban"

# URLs.
_SEARCH_URL = "https://search.douban.com/book/subject_search"
_SUBJECT_URL = "https://book.douban.com/subject"

# Default persistent browser profile (isolated from the user's main Chrome). Douban needs
# no login, but keeping a profile lets a one-time login / cookie survive if a page asks.
_DEFAULT_PROFILE = config_dir() / "douban-chrome"

# A book-card link selector, used as the positive-signal wait signal on the search page.
_CARD_SELECTOR = "a[href*='/subject/']"

# Matches the Douban subject id within a subject page/url (``subject/<id>``).
_SUBJECT_ID_RE = re.compile(r"subject/(\d+)")


# --- pure parsers (no browser / no network) ---

def _classify_meta(segs: list[str]) -> dict:
    """Classify the SSR ``abstract`` meta line (``作者 / 出版社 / 出版年 / 定价``) into fields.

    Douban packs the bibliographic one-liner into the search card's ``abstract`` field, e.g.
    ``李人宪 / 国防工业出版社 / 2008-3 / 22.00元``. Segments are identified by marker heuristics
    (publisher contains ``出版社``, price ends with ``元``, year is 4-digit) rather than by a
    fixed position, because some cards omit the author or the price.
    """
    meta = {"author": "", "publisher": "", "year": "", "price": ""}
    for s in segs:
        if "出版社" in s and not meta["publisher"]:
            meta["publisher"] = s
        elif "元" in s and not meta["price"]:
            meta["price"] = s
        elif (yr := extract_year(s)) and not meta["year"]:
            meta["year"] = yr
        elif not meta["author"]:
            meta["author"] = s
    return meta


def _split_slash(text: str) -> list[str]:
    """Split a ``/``-joined Douban field (SSR ``abstract``, author list) into cleaned segments."""
    return [s.strip() for s in (text or "").split("/") if s.strip()]


def _subject_id(card: dict) -> str:
    """Return the Douban subject id (as str) for a search card, from ``id``/``url``."""
    sid = card.get("id") if isinstance(card, dict) else None
    if sid is not None:
        return str(sid)
    m = _SUBJECT_ID_RE.search((card.get("url") or "") if isinstance(card, dict) else "")
    return m.group(1) if m else ""


def _card_to_item(card: dict, source: str = _SOURCE) -> AcademicItem | None:
    """Build one ``AcademicItem`` (book) from a search card (lenient)."""
    title = (card.get("title") or "").strip()
    if not title:
        return None
    url = (card.get("url") or "").strip()
    meta = _classify_meta(_split_slash(card.get("abstract")))
    creators: list[dict] = []
    if meta["author"]:
        # An author field may list several names separated by a CJK/comma; keep the whole
        # name for the one shown here (get() refines with the full author list).
        creators = [normalize_author_name(meta["author"])]
    key = _subject_id(card) or url or title
    extra: dict = {"douban": True, "card": card}
    if isinstance(card.get("rating"), dict):
        extra["douban_rating"] = card["rating"]
    return AcademicItem(
        source=source, key=key, title=title, creators=creators,
        year=meta["year"], url=url, publisher=meta["publisher"],
        item_type="book", extra=extra,
    )


def window_data_to_items(payload: dict | None, source: str = _SOURCE) -> list[AcademicItem]:
    """Pure: turn a parsed ``window.__DATA__`` payload into book ``AcademicItem`` list.

    Reads the ``items`` array of the search-page SSR payload (each card is a plain dict with
    title / url / id / rating / abstract). Missing entries are tolerated; non-dict cards and
    empty titles are skipped. Offline-testable.
    """
    out: list[AcademicItem] = []
    if not isinstance(payload, dict):
        return out
    for card in (payload.get("items") or []):
        if not isinstance(card, dict):
            continue
        it = _card_to_item(card, source)
        if it:
            out.append(it)
    return out


# Subject ``#info`` label -> AcademicItem field (the label regex already strips the colon).
_INFO_FIELDS = {"作者": "author", "出版社": "publisher", "出版年": "pubdate",
                "ISBN": "isbn", "页数": "pages", "定价": "price"}


def _parse_info(info_text: str) -> dict:
    """Parse a subject page's ``#info`` text into field dict (author/publisher/...).

    Format observed live: ``作者: 李人宪\n出版社: 国防工业出版社\n出版年: 2008-3\nISBN: 9787118055559``
    ``\n页数: 226\n定价: 22.00元``.
    ``#info`` is block-level, so ``innerText`` joins label/value pairs with ``\\n``
    (the canonical separator for this adapter). Each line is ``label: value`` (``:``
    ASCII or fullwidth). Returns a dict with keys author / publisher / pubdate / isbn /
    pages / price; missing keys are empty.
    """
    fields = {"author": "", "publisher": "", "pubdate": "", "isbn": "", "pages": "", "price": ""}
    for line in (info_text or "").split("\n"):
        m = re.match(r"^\s*([^：:]+)[：:]\s*(.*?)\s*$", line.strip())
        if not m:
            continue
        field = _INFO_FIELDS.get(m.group(1).strip())
        if field:
            fields[field] = m.group(2).strip()
    return fields


def _subject_to_item(title: str, url: str, subj_id: str, info: dict,
                     source: str = _SOURCE) -> AcademicItem:
    """Pure: build a complete book ``AcademicItem`` from subject-page fields (no browser).

    Gets() currently does not read the page's rating, so this builder takes only the
    bibliographic fields; the search path (:func:`_card_to_item`) attaches the card rating.
    """
    creators = [normalize_author_name(a) for a in _split_slash(info.get("author", ""))]
    creators = [c for c in creators if c.get("family") or c.get("given")]
    return AcademicItem(
        source=source, key=subj_id or url or title, title=title, creators=creators,
        year=extract_year(info.get("pubdate", "")), url=url,
        publisher=info.get("publisher", ""), isbn=info.get("isbn", ""),
        page=info.get("pages", ""), item_type="book", extra={"douban": True},
    )


# --- Playwright adapter ---

class DoubanBackend(PlaywrightBackend):
    """Douban Chinese book metadata search via Playwright (read-only, headless by default)."""

    DEFAULT_PROFILE = _DEFAULT_PROFILE
    INSTALL_HINT = _PLAYWRIGHT_HINT + " (or re-run install.sh --with-douban)"

    def __init__(self, name: str = "douban", user_data_dir=None, headless: bool = True,
                 timeout: int = 30, max_results: int = 10):
        # Headless by default: Douban's search SSR carries the card JSON (no slider captcha).
        super().__init__(name=name, user_data_dir=user_data_dir, headless=headless,
                         timeout=timeout, max_results=max_results)

    @staticmethod
    def _search_url(query: str) -> str:
        return f"{_SEARCH_URL}?{urlencode({'search_text': query})}"

    @staticmethod
    def _subject_url(key: str) -> str:
        """Normalize a ``subject/<id>`` / ``douban/<id>`` / url / bare id into a subject URL."""
        key = (key or "").strip()
        # ``data get`` keys are shown in the display form ``douban/<id>``; strip our own
        # prefix so a ``get`` routes straight to the subject page.
        if key.startswith(f"{_SOURCE}/"):
            key = key[len(f"{_SOURCE}/"):]
        if key.startswith("http"):
            return key
        m = _SUBJECT_ID_RE.search(key)
        if m:
            return f"{_SUBJECT_URL}/{m.group(1)}/"
        if key.isdigit():
            return f"{_SUBJECT_URL}/{key}/"
        raise KeyError(f"[{_SOURCE}] cannot build subject URL from key: {key!r}")

    def _read_payload(self, page) -> dict:
        """Return the parsed ``window.__DATA__`` (a dict). Tolerates a JSON-string or None."""
        raw = page.evaluate("() => window.__DATA__ ?? null")
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except Exception:
                return {}
        return {}

    def _wait_for_info(self, page, timeout: int | None = None) -> str:
        """Block until the subject page's ``#info`` has non-empty text (or the timeout);
        return that text (stripped) so :meth:`get` need not read ``#info`` a second time.

        ``#info`` exists in the SSR shell but stays empty until JavaScript fills it, so an
        existence-based wait is a false signal; we poll for actual content instead.
        """
        return self._wait_until(
            page,
            lambda p: (p.eval_on_selector("#info", "el => el ? el.innerText : ''") or "").strip(),
            timeout,
        ) or ""

    def search(self, query: str, limit: int = 10, filters: dict | None = None) -> list[AcademicItem]:
        """Search Douban books and return candidate cards (title/author/publisher/year/rating)."""
        filters = self._filters(filters)
        # Douban's free query takes any words; fold title/author into it (post-filter below).
        q = " ".join(t for t in (query, filters.get("title"), filters.get("author")) if t)
        n = self._clamp_limit(limit)
        with self._browser() as page:
            page.goto(self._search_url(q), timeout=self._timeout * 1000, wait_until="domcontentloaded")
            self._wait_for_signal(page, _CARD_SELECTOR)
            payload = self._read_payload(page)
            items = window_data_to_items(payload)[:n]
            return self._filter_year(items, filters)

    def get(self, key: str) -> AcademicItem:
        """Fetch a single book's full metadata (author list / ISBN / publisher / pages)."""
        if not key:
            raise KeyError(f"[{self.name}] empty key")
        url = self._subject_url(key)
        with self._browser() as page:
            page.goto(url, timeout=self._timeout * 1000, wait_until="domcontentloaded")
            info_text = self._wait_for_info(page)
            if not info_text:
                raise KeyError(f"[{self.name}] could not read subject page (blocked / missing): {key}")
            title = page.eval_on_selector("#wrapper h1 span", "el => el ? el.innerText : ''") or ""
            if not title:
                raise KeyError(f"[{self.name}] could not read subject page (blocked / missing): {key}")
            info = _parse_info(info_text)
            subj_id = _subject_id({"url": url})
            return _subject_to_item(title, url, subj_id, info)
