"""One long-lived, real Chrome the entitled readers fetch through.

Copying clearance cookies by hand is what this replaces: a Cloudflare/WAF clearance is bound
to the user agent *and* the egress IP, and it expires in minutes, so every copy is a race.
A browser that is already signed in keeps its own session, renews it, and passes the gates by
being a browser — ScienceDirect rejects even headless Chromium (verified 2026-11-24), so the
page itself is fetched through here and the cookies are never lifted into ``httpx``.

The instance is shared by every site and by every command: :meth:`Session.start` reuses a
running one (its pid lives in a discovery file) and :meth:`Session.close` removes the file.
``launcher`` is injected so the whole module is testable without a browser.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from . import paths
from .errors import External


class BrowserUnavailable(External):
    """No usable browser: Playwright missing, the browser not downloaded, or nothing running."""

    exit_code = 2


@dataclass
class PageResponse:
    """What :meth:`Session.page` hands back — the four fields callers of a page need."""

    status: int
    text: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)


class _Runtime:
    """The live objects a launcher returns, so a caller can close both the context and the driver."""

    def __init__(self, context, stopper=None):
        self.context = context
        self._stopper = stopper

    def stop(self) -> None:
        try:
            self.context.close()
        finally:
            if self._stopper:
                self._stopper()


def _default_launcher(profile, headless, channel, proxy) -> _Runtime:
    """Start a persistent Chrome against ``profile``; real Chrome unless ``channel`` is unset."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise BrowserUnavailable(
            "Playwright is not installed. Install it with `litcli backends install-browser` "
            "and retry (or fall back to a configured cookie + user_agent).") from e
    paths.apply_browser_cache()
    engine = sync_playwright().start()
    kw: dict = {"headless": headless, "viewport": {"width": 1360, "height": 900}}
    if channel:
        kw["channel"] = channel
    if proxy:
        # The clearance is bound to the egress IP, so the browser must leave by the same proxy
        # as httpx or every cookie it earned is worthless to the request path.
        kw["proxy"] = {"server": proxy}
    try:
        context = engine.chromium.launch_persistent_context(str(profile), **kw)
    except Exception as e:
        engine.stop()
        raise BrowserUnavailable(
            f"could not start the browser (run `litcli backends install-browser` first): {e}",
            hint="litcli backends install-browser") from e
    return _Runtime(context, engine.stop)


class Session:
    """A shared, persistent Chrome. Cheap to construct; :meth:`start` is the expensive part."""

    def __init__(self, profile: Path | None = None, headless: bool = False,
                 channel: str | None = None, proxy: str | None = None, launcher=None):
        self._profile = Path(profile).expanduser() if profile else paths.browser_profile_dir()
        self._headless = headless
        self._channel = channel
        self._proxy = proxy
        self._launcher = launcher or _default_launcher
        self._runtime: _Runtime | None = None
        self._owned = False

    # -- lifetime --------------------------------------------------------------

    def alive(self) -> bool:
        """True while the discovery file names a process that still exists."""
        try:
            info = json.loads(paths.browser_session_file().read_text(encoding="utf-8"))
            os.kill(int(info["pid"]), 0)
        except (OSError, ValueError, KeyError, TypeError):
            return False
        return True

    def start(self) -> None:
        """Launch the browser unless one is already running, then record where it is."""
        if self.alive():
            return
        self._profile.mkdir(parents=True, exist_ok=True)
        self._runtime = self._launcher(self._profile, self._headless, self._channel, self._proxy)
        self._owned = True
        paths.browser_session_file().parent.mkdir(parents=True, exist_ok=True)
        paths.browser_session_file().write_text(
            json.dumps({"pid": os.getpid(), "profile": str(self._profile)}), encoding="utf-8")

    def close(self) -> None:
        """Tear down only what this instance started; a borrowed session stays up."""
        if self._runtime and self._owned:
            self._runtime.stop()
        self._runtime = None
        self._owned = False
        if paths.browser_session_file().exists() and not self.alive_elsewhere():
            paths.browser_session_file().unlink(missing_ok=True)

    def alive_elsewhere(self) -> bool:
        """Alive and owned by a process other than this one."""
        try:
            info = json.loads(paths.browser_session_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return int(info.get("pid", 0)) != os.getpid()

    # -- work ------------------------------------------------------------------

    def _page(self):
        if not self.alive():
            raise BrowserUnavailable(
                "no browser session is running — run `litcli browser login <site>` first "
                "(or configure a cookie + user_agent to use the HTTP path).",
                hint="litcli browser login")
        if self._runtime is None:  # attached to a session another process owns
            raise BrowserUnavailable("the browser session belongs to another process")
        page = self._runtime.context.pages[0] if self._runtime.context.pages \
            else self._runtime.context.new_page()
        return page

    def page(self, url: str) -> PageResponse:
        """Navigate and hand back the answer (a gate page included; it is not an error here)."""
        page = self._page()
        response = page.goto(url)
        return PageResponse(status=response.status, text=page.content(), url=response.url,
                            headers=dict(response.headers))

    def download(self, url: str) -> bytes:
        """Navigate to a file URL and return its bytes."""
        page = self._page()
        with page.expect_download() as caught:
            page.goto(url)
        return Path(caught.value.path).read_bytes()
