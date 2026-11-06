"""Shared Playwright base for browser-driven backends (CNKI / Douban).

These backends drive a real Chromium (via Playwright) because their data is rendered
by JavaScript (and, for CNKI, gated behind a login/slider captcha). The network layer is
Playwright, not ``urllib``, so they do not inherit :func:`base.retry_json`. This base
owns the browser lifecycle and the lazy-import error surface; each subclass supplies its
own persistent profile dir, result-row selectors, and pure parser.

Compliance: these backends only read *public* metadata (title-metadata for CNKI,
bibliographic fields for Douban). They never bypass captchas programmatically — when a
site presents a captcha they either wait for the human (CNKI) or surface a clear error.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from ..paths import config_dir, is_frozen
from .base import Backend

# Install hint used when Playwright is missing (generic; subclasses tailor it with the
# matching `install.sh --with-*` flag).
_PLAYWRIGHT_HINT = ("pip install --break-system-packages --user playwright && "
                    "playwright install chromium")


class PlaywrightBackend(Backend):
    """Backend that renders pages through a persistent-visible (or headless) Chromium.

    ``DEFAULT_PROFILE`` / ``INSTALL_HINT`` are class attributes a subclass overrides:
    the profile dir is where the user's browser session (login, cookies) persists so it
    survives across calls, and the install hint tells the user which flag installs the
    Playwright dependency.
    """

    DEFAULT_PROFILE: Path = config_dir() / "playwright-chrome"
    INSTALL_HINT: str = _PLAYWRIGHT_HINT

    def __init__(self, name: str = "playwright", user_data_dir=None, headless: bool = False,
                 timeout: int = 30, max_results: int = 10):
        self.name = name or self.__class__.__name__.lower()
        self._profile = Path(user_data_dir).expanduser() if user_data_dir else self.DEFAULT_PROFILE
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
            # A binary build ships the package and downloads the browser separately.
            hint = "litcli backends install-browser" if is_frozen() else self.INSTALL_HINT
            raise RuntimeError(
                f"[{self.name}] Playwright is not installed. Install it with `{hint}`, then retry."
            ) from e

    @contextmanager
    def _browser(self):
        """Open a persistent Chromium context (so the user's login survives), yield the
        first page, and always tear down the context + engine. Resource ownership lives
        here, not in each caller."""
        sync_playwright = self._playwright()
        pw = sync_playwright().start()
        context = None
        try:
            try:
                context = pw.chromium.launch_persistent_context(
                    str(self._profile), headless=self._headless,
                    viewport={"width": 1360, "height": 900},
                )
            except Exception as e:  # the browser was never downloaded
                hint = ("litcli backends install-browser" if is_frozen()
                        else "playwright install chromium")
                raise RuntimeError(f"[{self.name}] browser missing — run `{hint}` first: {e}") from e
            page = context.pages[0] if context.pages else context.new_page()
            yield page
        finally:
            if context:
                context.close()
            pw.stop()

    def _detect_captcha(self, page) -> bool:
        """Return True if the page is a captcha / verification wall. Douban has none;
        CNKI overrides this block-puzzle detection."""
        return False

    def _wait_until(self, page, predicate, timeout: int | None = None):
        """Poll ``predicate(page)`` until it returns a truthy value (or the timeout elapses).

        This is the single poll skeleton shared by :meth:`_wait_for_signal` (selector
        existence) and a backend's own content waits (e.g. Douban ``#info``). It runs the
        predicate in Python each tick and returns the value once truthy (so a content wait
        can hand back the captured text, avoiding a redundant read); a falsy result just
        keeps polling, and the timeout elapses quietly (no raise). The 0.5s cadence and
        per-tick exception tolerance mirror the original behavior, so the existing
        browser-backend tests stay green.
        """
        import time
        deadline = time.monotonic() + (timeout or self._timeout)
        value = None
        while time.monotonic() < deadline:
            try:
                value = predicate(page)
                if value:
                    return value
            except Exception:
                pass
            time.sleep(0.5)
        return value

    def _wait_for_signal(self, page, selector: str, timeout: int | None = None):
        """Block until at least one node matching ``selector`` renders (or the timeout
        elapses). Results render asynchronously, so a fixed sleep is wasteful when fast
        and too short when slow; a positive signal is both cheap and robust, and an empty
        page just times out quietly. Returns the matched nodes (truthy once found)."""
        return self._wait_until(page, lambda p: p.query_selector_all(selector), timeout)

    def _clamp_limit(self, limit: int | None) -> int:
        """Cap a requested ``limit`` to the backend max (mirrors the per-month crawl cap)."""
        return min(limit or self._max, self._max)

    def _filter_year(self, items: list, filters: dict | None) -> list:
        """Best-effort post-filter of items to a specific ``year`` from ``filters``.

        These sites expose no stable date filter syntax, so the field filter is applied
        after the free-query fetch (shared by CNKI and Douban).
        """
        yr = (filters or {}).get("year")
        return [it for it in items if it.year == yr] if yr else items
