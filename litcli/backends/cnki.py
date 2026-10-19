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

import re
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urljoin, urlencode

from ..models import AcademicItem, extract_year, normalize_author_name
from .base import Backend

# Product version, used only by the pure parser for the AcademicItem.extra provenance.
_SOURCE = "cnki"

# Site origin used to absolute-ize result links (see :func:`_abs_url`).
_SITE_ORIGIN = "https://kns.cnki.net"

# URL to the CNKI search results page (kns8s = default advanced-search UI).
_SEARCH_URL = "https://kns.cnki.net/kns8s/defaultresult/index"

# Default persistent browser profile directory (isolated from the user's main Chrome).
_DEFAULT_PROFILE = Path.home() / ".config" / "litcli" / "cnki-chrome"

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


class CnkiBackend(Backend):
    """CNKI metadata search via Playwright (read-only, visible browser + own session)."""

    def __init__(self, name: str = "cnki", user_data_dir=None, headless: bool = False,
                 timeout: int = 30, max_results: int = 10):
        self.name = name
        self._profile = Path(user_data_dir).expanduser() if user_data_dir else _DEFAULT_PROFILE
        self._profile.mkdir(parents=True, exist_ok=True)
        self._headless = headless
        self._timeout = timeout
        self._max = max_results

    @property
    def capabilities(self) -> set[str]:
        return {"read"}

    # --- playwright import is deferred so the core pip install stays zero-dep ---

    def _playwright(self):
        """Import Playwright lazily; raise a clear, actionable error when missing."""
        try:
            from playwright.sync_api import sync_playwright
            return sync_playwright
        except ImportError as e:
            raise RuntimeError(
                f"[{self.name}] Playwright is not installed. Install it with "
                f"`{_INSTALL_HINT}` (or re-run install.sh --with-cnki), then retry."
            ) from e

    def _page_url(self, query: str) -> str:
        return f"{_SEARCH_URL}?{urlencode({'kw': query})}"

    @contextmanager
    def _browser(self):
        """Open a persistent Chromium context (so the user's CNKI login survives), yield the
        first page, and always tear down the context + engine. Raises a clear message when
        Playwright is missing. Resource ownership lives here, not in each caller."""
        sync_playwright = self._playwright()
        pw = sync_playwright().start()
        context = None
        try:
            context = pw.chromium.launch_persistent_context(
                str(self._profile), headless=self._headless,
                viewport={"width": 1360, "height": 900},
            )
            page = context.pages[0] if context.pages else context.new_page()
            yield page
        finally:
            if context:
                context.close()
            pw.stop()

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
        import time
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self._detect_captcha(page):
                return
            time.sleep(1.0)

    def _wait_for_results(self, page, timeout: int | None = None) -> None:
        """Block until at least one result row renders (or the timeout elapses).

        Results render asynchronously after the search request, so a fixed sleep is
        wasteful when fast and too short when slow. Waiting on a positive signal (a row
        node) is both cheap and robust; an empty result page just times out quietly.
        """
        import time
        deadline = time.monotonic() + (timeout or self._timeout)
        while time.monotonic() < deadline:
            try:
                if page.query_selector_all(_ROW_SELECTOR):
                    return
            except Exception:
                pass
            time.sleep(0.5)

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

    def search(self, query: str, limit: int = 10, filters: dict | None = None) -> list[AcademicItem]:
        """Search CNKI and return normalized title metadata (read-only)."""
        filters = self._filters(filters)
        # CNKI has no stabilized field query we can rely on; fold title/author words into
        # the free query so a broad hit is still narrowed (post-filter below is optional).
        terms = [t for t in (query, filters.get("title"), filters.get("author"),
                             filters.get("container")) if t]
        q = " ".join(terms)
        n = min(limit or self._max, self._max)
        with self._browser() as page:
            page.goto(self._page_url(q), timeout=self._timeout * 1000, wait_until="domcontentloaded")
            if self._detect_captcha(page) and not self._headless:
                # Visible browser: let the human solve the slider, then wait for the results.
                self._wait_captcha_finished(page)
            self._wait_for_results(page)
            rows = self._virtualize_rows(page, limit=n)
            items = rows_to_items(rows)
            # (Optional) honor a --year filter as a post-filter since CNKI's query syntax
            # is unstable; the rest is left to the caller.
            if filters.get("year"):
                yr = filters["year"]
                items = [it for it in items if it.year == yr]
            return items

    def get(self, key: str) -> AcademicItem:
        """Fetch a single record by URL. Best-effort: open the detail page and parse.

        CNKI detail pages can gate content behind the beta login; if the page lands on a
        captcha or login form and we cannot parse a title, raise a clear error rather than
        fabricate a record.
        """
        if not key:
            raise KeyError(f"[{self.name}] empty key")
        with self._browser() as page:
            url = key if key.startswith("http") else f"{_SEARCH_URL}?kw={key}"
            page.goto(url, timeout=self._timeout * 1000, wait_until="domcontentloaded")
            if self._detect_captcha(page) and not self._headless:
                self._wait_captcha_finished(page)
            self._wait_for_results(page)
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


_INSTALL_HINT = "pip install --break-system-packages --user playwright && playwright install chromium"
