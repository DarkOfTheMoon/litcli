"""CNKI (China National Knowledge Infrastructure) title-metadata retrieval backend.

Read-only. Drives a visible Chromium via Playwright to search CNKI and extract public
title metadata (title / authors / journal / year / abstract snippet) from the result
list page. It does NOT download full-text (paywalled) — that stays out of scope and is
surfaced by the legitimate fulltext path.

The extraction is split in two:

* a *pure* function :func:`rows_to_items` that turns a list of "virtual rows"
  (plain dicts with title/author/container/year/abstract/url) into ``AcademicItem``
  objects. It never touches a browser, so it is unit-testable offline and survives CNKI
  markup changes (only the adapter below changes).

* a thin Playwright *adapter* (:class:`CnkiBackend`) that launches a persistent
  (user-logged-in) browser session, navigates the CNKI search page, detects the slider
  captcha, waits for the human to solve it, and virtualizes each result row into the
  dicts :func:`rows_to_items` consumes.

Compliance: CNKI blocks scripted / headless access with a block-puzzle captcha (verified
by probe: ``kns.cnki.net/.../defaultresult/index`` redirects to ``/verify/home``). This
backend therefore requires a *visible* browser and reuses the user's own logged-in
session; it does not programmatically bypass the captcha or scrape paywalled full-text.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import urlencode, urljoin

from ..models import AcademicItem, extract_year, normalize_author_name
from ..paths import config_dir
from .playwright import _PLAYWRIGHT_HINT, PlaywrightBackend

# Product version, used only by the pure parser for the AcademicItem.extra provenance.
_SOURCE = "cnki"

# Site origin used to absolute-ize result links (see :func:`_abs_url`).
_SITE_ORIGIN = "https://kns.cnki.net"

# URL to the CNKI search results page (kns8s = default advanced-search UI).
_SEARCH_URL = "https://kns.cnki.net/kns8s/defaultresult/index"

# Default persistent browser profile directory (isolated from the user's main Chrome).
_DEFAULT_PROFILE = config_dir() / "cnki-chrome"

# Result row cell selectors, per field. Kept as one table so a CNKI markup change (e.g.
# ``td.author`` -> ``td.authors``) is a one-line edit here, not a hunt across methods.
_ROW_CELLS = {
    "title": ("td.name a", "td.name", "a"),
    "author": ("td.author a", ".author a"),
    "container": ("td.source", ".source"),
    "year": ("td.date", ".date"),
}

# Result-row node selector (shared by virtualizing and by the positive-signal wait below).
_ROW_SELECTOR = ".result-table-list tbody tr, .result-list tbody tr, .list2_table tbody tr, div.result-table-item"

# CJK ideographs (BMP, extension A, compatibility) - the runs the query term splitter targets.
_CJK_RUN_RE = re.compile(r"[㐀-䶿一-鿿豈-﫿]+")
#: A CJK run shorter than this is one word or a short fixed term CNKI matches as-is.
_MIN_SPLIT = 5
#: Boolean operators a user may already have put in the query (left as typed).
_OPERATOR_RE = re.compile(r"[*+]|\b(?:AND|OR|NOT)\b", re.IGNORECASE)

# jieba's segmenter, imported on first use (it loads a dictionary, so never at module import).
_lcut = None


def _segment(run: str) -> str:
    """Space-separate one CJK run into words (jieba, or the run unchanged when unavailable)."""
    global _lcut
    if _lcut is None:
        try:
            import jieba

            jieba.setLogLevel(logging.INFO)  # first cut logs "Building prefix dict" otherwise
            _lcut = jieba.lcut
        except ImportError:  # optional dependency; the caller still gets a usable query
            _lcut = False
    if not _lcut:
        return run
    return " ".join(word for word in _lcut(run) if word.strip())


def split_terms(query: str) -> str:
    """Split concatenated Chinese keywords into space-separated words.

    CNKI's simple ``kw`` box matches the whole string as a single term, so
    ``风电叶片复合材料疲劳损伤寿命预测`` returns nothing while
    ``风电叶片 复合材料 疲劳损伤 寿命预测`` matches. Every CJK run of ``_MIN_SPLIT`` chars or
    more is segmented; explicit separators, operators and Latin text are left untouched.
    """
    out = _CJK_RUN_RE.sub(lambda m: _segment(m.group(0))
                          if len(m.group(0)) >= _MIN_SPLIT else m.group(0), query or "")
    return " ".join(out.split())


def kw_expression(query: str) -> str:
    """Turn a query into a CNKI ``kw`` expression that ANDs its terms.

    In ``kw``, whitespace and ``+`` mean *or* (an energy query over four terms yielded
    705,032 hits led by unrelated recent papers), while ``*`` means *and* (the same query
    with ``*`` yielded 16 hits, all on topic). So the split words are joined with ``*``.
    A query that already carries an operator (``*``, ``+``, AND/OR/NOT) is left as the user
    wrote it, whitespace included.
    """
    spaced = split_terms(query)
    if _OPERATOR_RE.search(spaced):
        return spaced
    return "*".join(spaced.split())


def _row_to_item(row: dict, source: str = _SOURCE) -> AcademicItem:
    """Build one ``AcademicItem`` from a virtual row dict (lenient)."""
    title = (row.get("title") or "").strip()
    url = (row.get("url") or "").strip()
    authors = [normalize_author_name(a) for a in (row.get("author") or [])]
    artists: list[dict] = [a for a in authors if a.get("family")]
    year = (row.get("year") or "").strip()
    return AcademicItem(
        source=source,
        key=url or title,
        title=title,
        creators=artists,
        year=year,
        abstract=(row.get("abstract") or "").strip(),
        doi="",
        url=url,
        container_title=(row.get("container") or "").strip(),
        item_type="article",
        extra={"cnki": True, "row": row},
    )


def rows_to_items(rows: list[dict], source: str = _SOURCE) -> list[AcademicItem]:
    """Pure: turn virtual result rows into ``AcademicItem`` list (no browser I/O).

    Each ``row`` carries title / author (list of str) / container / year / abstract / url;
    missing entries are tolerated. Suitable for offline unit tests and for re-parsing after
    a markup change.
    """
    out: list[AcademicItem] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        if not (row.get("title") or "").strip():
            continue
        out.append(_row_to_item(row, source))
    return out


class CnkiBackend(PlaywrightBackend):
    """CNKI metadata search via Playwright (read-only, visible browser + own session)."""

    DEFAULT_PROFILE = _DEFAULT_PROFILE
    INSTALL_HINT = _PLAYWRIGHT_HINT + " (or re-run install.sh --with-cnki)"

    def __init__(self, name: str = "cnki", user_data_dir=None, headless: bool = False,
                 timeout: int = 30, max_results: int = 10):
        super().__init__(name=name, user_data_dir=user_data_dir, headless=headless,
                         timeout=timeout, max_results=max_results)

    def _page_url(self, query: str) -> str:
        return f"{_SEARCH_URL}?{urlencode({'kw': kw_expression(query)})}"

    def _detect_captcha(self, page) -> bool:
        """Return True if the page is a CNKI slider captcha (``/verify/`` URL or slider text)."""
        try:
            url = page.url
            if "/verify/" in url:
                return True
            body = page.evaluate("() => document.body?.innerText || ''") or ""
            return "安全验证" in body or "向右滑动" in body
        except Exception:
            return False

    def _wait_captcha_finished(self, page, timeout: int = 120):
        """Block until the human solves the captcha and the search page loads.

        Ping the page URL; once it no longer reports 安全验证 and lands on the results
        page, the slider (and login) is done. The user does it visibly by hand.
        """
        self._wait_until(page, lambda p: not self._detect_captcha(p), timeout)

    def _virtualize_rows(self, page, limit: int) -> list[dict]:
        """Scrape the result list page into virtual row dicts (pure parser input).

        The CNKI DOM here is best-effort and version-sensitive, so this is a thin adapter:
        it locates candidate result nodes, reads their text/link, and strips boilerplate.
        Rewrite this function if CNKI changes markup; :func:`rows_to_items` stays intact.
        """
        rows: list[dict] = []
        try:
            items = page.query_selector_all(_ROW_SELECTOR)
        except Exception:
            items = []
        for node in (items or [])[:limit]:
            try:
                if node is None:
                    continue
                title, url = self._title_value(node)
                if not title:
                    continue
                authors = self._author_values(node)
                rows.append({
                    "title": title,
                    "author": authors,
                    "container": self._cell_text(node, "container"),
                    "year": extract_year(self._cell_text(node, "year")),
                    "abstract": "",
                    "url": _abs_url(url),
                })
            except Exception:
                continue
        return rows

    def _inner_text(self, node, sel: str) -> str:
        """Return stripped inner text of the first ``sel`` match under ``node`` (or '' if none)."""
        try:
            el = node.query_selector(sel)
            if el is not None:
                return el.inner_text().strip()
        except Exception:
            pass
        return ""

    def _title_value(self, node) -> tuple[str, str]:
        """Return (title, href) from the title cell (try ``_ROW_CELLS['title']`` selectors)."""
        for sel in _ROW_CELLS["title"]:
            el = node.query_selector(sel)
            if el is not None:
                txt = el.inner_text().strip()
                if txt:
                    return txt, el.get_attribute("href") or ""
        # Last resort: outer text first line.
        return node.inner_text().split("\n")[0].strip(), ""

    def _author_values(self, node) -> list[str]:
        """Collect author names (unique) from ``_ROW_CELLS['author']`` (<a> per author)."""
        out: list[str] = []
        try:
            links = node.query_selector_all(", ".join(_ROW_CELLS["author"]))
        except Exception:
            links = []
        for a in links or []:
            try:
                name = a.inner_text().strip()
            except Exception:
                continue
            if name and name not in out:
                out.append(name)
        return out

    def _cell_text(self, node, field: str) -> str:
        """Return stripped inner text of the first ``_ROW_CELLS[field]`` match under ``node``."""
        for sel in _ROW_CELLS[field]:
            txt = self._inner_text(node, sel)
            if txt:
                return txt
        return ""

    def _open_results(self, page, url: str) -> None:
        """Load a CNKI page and block until the result rows render.

        If a slider captcha appears and we are in a visible browser (the user's own logged-in
        session), pause so the human solves it; otherwise just wait for the result nodes.
        """
        page.goto(url, timeout=self._timeout * 1000, wait_until="domcontentloaded")
        if self._detect_captcha(page) and not self._headless:
            # Visible browser: let the human solve the slider, then wait for the results.
            self._wait_captcha_finished(page)
        self._wait_for_signal(page, _ROW_SELECTOR)

    def search(self, query: str, limit: int = 10, filters: dict | None = None) -> list[AcademicItem]:
        """Search CNKI and return normalized title metadata (read-only).

        Concatenated Chinese keywords are split into words and AND-joined
        (:func:`kw_expression`); CNKI would otherwise match the whole run as one term and
        find nothing, and its plain whitespace separator only ORs the words.
        """
        filters = self._filters(filters)
        # CNKI has no stabilized field query we can rely on; fold title/author words into
        # the free query so a broad hit is still narrowed (post-filter below is optional).
        terms = [t for t in (query, filters.get("title"), filters.get("author"),
                             filters.get("container")) if t]
        q = " ".join(terms)
        n = self._clamp_limit(limit)
        with self._browser() as page:
            self._open_results(page, self._page_url(q))
            rows = self._virtualize_rows(page, limit=n)
            items = rows_to_items(rows)
            # (Optional) honor a --year filter as a post-filter since CNKI's query syntax
            # is unstable; the rest is left to the caller.
            return self._filter_year(items, filters)

    def get(self, key: str) -> AcademicItem:
        """Fetch a single record by URL. Best-effort: open the detail page and parse.

        CNKI detail pages can gate content behind the beta login; if the page lands on a
        captcha or login form and we cannot parse a title, raise a clear error rather than
        fabricate a record.
        """
        if not key:
            raise KeyError(f"[{self.name}] empty key")
        with self._browser() as page:
            url = key if key.startswith("http") else self._page_url(key)
            self._open_results(page, url)
            title = page.title()
            if not title or "安全验证" in title:
                raise KeyError(f"[{self.name}] could not read detail page for key (captcha or login): {key}")
            return AcademicItem(source=self.name, key=key, title=title, url=url, item_type="article")


def _abs_url(href: str) -> str:
    """Absolute-ize a (possibly relative) CNKI link against the site origin."""
    href = (href or "").strip()
    if not href:
        return ""
    return urljoin(_SITE_ORIGIN, href)
