"""One long-lived, real Chrome the entitled readers fetch through.

Copying clearance cookies by hand is what this replaces: a Cloudflare/WAF clearance is bound
to the user agent *and* the egress IP, and it expires in minutes, so every copy is a race.
A browser that is already signed in keeps its own session, renews it, and passes the gates by
being a browser — ScienceDirect rejects even headless Chromium (verified 2026-11-24), so the
page itself is fetched through here and the cookies are never lifted into ``httpx``.

The instance is shared by every site and by every command: the browser belongs to a broker
(``litcli.browser_broker``) that one process per profile keeps, and a :class:`Session` is a
client holding a lease on it. ``lease_factory`` is injected so the whole module is testable
without a browser.
"""
from __future__ import annotations

import contextlib
import shutil
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from . import browser_broker, debug, paths
from .backends._cf_gate import is_challenge_page
from .browser_broker import _CHROME_BINARIES, BrowserUnavailable, _attach_engine
from .errors import Unavailable


def _still_coming(page) -> bool:
    """Whether the page is still the challenge rather than something to read.

    Mid-exchange ``content()`` raises; an unreadable page is the exchange in progress, not an
    answer, so it counts as still coming. Anything else — an inline viewer, an article page, a
    sign-in page — is a document, and a document is the answer whether the bytes are in it or
    the lack of entitlement is.
    """
    try:
        return is_challenge_page(page.content())
    except PlaywrightError:
        return True


def _site_root(url: str) -> str:
    """The origin of ``url`` (scheme + host), the page a sign-in clears first."""
    return urlparse(str(url or ""))._replace(path="/", query="", fragment="").geturl()


def looks_like_pdf(url: str) -> bool:
    """True when ``url`` is a file's own address.

    After a navigation lands on a PDF, Chrome keeps that file's address in the page URL: it is
    how a route that turned into the file is told from one that bounced back to a page (``/abs/``,
    a sign-in page), and where the bytes then come from.
    """
    return urlparse(str(url or "")).path.lower().endswith(".pdf")


#: The ceiling on the page work of one item. A clock cannot tell "no entitlement" from "still
#: downloading" — the document can, so the wait ends the moment the page is a document rather
#: than a challenge; this only bounds a challenge that never resolves. It is one clock for the
#: whole item (navigation, challenge, then the last-resort request), not one per stage: three
#: stages of five minutes each held a run for a quarter of an hour on a single dead link. The
#: awaited transfer itself is not timed; it ends when the artifact lands or Chrome gives up.
_DOWNLOAD_WAIT = 300.0

#: The navigation's share of :data:`_DOWNLOAD_WAIT`. It waits only for the commit, so this is
#: ample for the ones that do answer; an answer that never comes still leaves most of the clock
#: to the fallback request, which is where a dead navigation's file can still turn up.
_NAV_WAIT = 60.0

#: The least the last-resort request gets when the clock above is spent — the budget the
#: navigation had. A request cut to the leftover second is as good as not made, and it can only
#: be reached after the loop has already waited the rest out.
_FALLBACK_WAIT = 60.0

#: How long a viewer shell under the route it was asked for is given to redirect into the file
#: (~2 s in practice; a bot check in between takes longer). Past it the shell is taken as the
#: answer, so an in-place HTML refusal does not sit out the whole ceiling above.
_REDIRECT_GRACE = 30.0

try:  # optional dependency: the module stays importable without Playwright
    from playwright._impl._errors import TargetClosedError  # not exported by sync_api
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
except ImportError:  # pragma: no cover - exercised through the missing-Playwright test
    class PlaywrightTimeoutError(Exception):  # type: ignore[no-redef]
        """Stand-in so :meth:`Session.download` reads the same either way."""

    PlaywrightError = OSError  # type: ignore[misc]
    TargetClosedError = OSError  # type: ignore[misc]


#: Messages that mean the browser or its tab went away rather than the page answering badly.
_WENT_AWAY = ("Target closed", "has been closed", "has been disconnected",
              "Browser closed", "Context closed")


def _went_away(e: BaseException) -> bool:
    """Whether ``e`` is the browser leaving, which one rebind can repair."""
    return isinstance(e, TargetClosedError) or any(hint in str(e) for hint in _WENT_AWAY)


@dataclass
class PageResponse:
    """What :meth:`Session.page` hands back — the fields callers of a page need.

    Shaped like the ``httpx`` response the HTTP path returns, so a reader can switch between the
    two without caring which one it got: the same ``status``/``status_code``, ``text``,
    ``content``, ``url`` and ``headers``. ``history`` is empty (the browser follows redirects
    inside ``goto`` and only the landing is observable).
    """

    status: int
    text: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def status_code(self) -> int:
        return self.status

    @property
    def content(self) -> bytes:
        return self.text.encode("utf-8", "replace")

    @property
    def history(self) -> tuple:
        return ()


class _Runtime:
    """The client-side pair behind a lease: a CDP context and the way to let it go.

    The browser is the broker's, so ``stop`` only detaches the driver; the port is kept so the
    session can still name the broker after the context has gone.
    """

    def __init__(self, context, stopper=None, port=None):
        self.context = context
        self._stopper = stopper
        self.port = port

    def stop(self) -> None:
        try:
            if not self.port:  # no broker to talk to: closing the context is all there is
                self.context.close()
        finally:
            if self._stopper:
                self._stopper()


def session_from_config(cfg: dict, *, pinned: bool = False) -> Session | None:
    """The session ``[browser]`` asks for, or ``None`` when the section is off.

    ``[network].proxy`` is passed on: the clearance is bound to the egress IP, so the browser
    must leave by the same proxy as the request path. Without a real Chrome the ``chrome``
    channel is dropped (Playwright would refuse to launch) and the bundled Chromium is used.
    """
    section = cfg.get("browser") or {}
    if not section.get("enabled"):
        return None
    channel = section.get("channel", "chrome")
    if channel and not any(shutil.which(binary) for binary in _CHROME_BINARIES):
        debug.warn("no Chrome found; the browser session falls back to the bundled Chromium")
        channel = None
    profile = section.get("profile") or None
    if profile is None and _dedicated_profile_left_behind():
        debug.warn('the browser session now uses your own Chrome profile and the old '
                   'dedicated one is idle; set `profile = "dedicated"` to keep its sign-ins')
    return Session(profile=profile,
                   headless=bool(section.get("headless", False)), channel=channel,
                   proxy=(cfg.get("network") or {}).get("proxy"),
                   profile_directory=section.get("profile_directory") or "Default",
                   profile_source="config" if profile else "default", pinned=pinned,
                   idle_minutes=float(section.get("idle_minutes", 10.0)))


def _dedicated_profile_left_behind() -> bool:
    """Whether the old dedicated profile holds a real Chrome cookie store."""
    try:
        return (paths.browser_profile_dir() / "Default" / "Cookies").stat().st_size > 0
    except OSError:
        return False


#: Returned by :func:`_goto_allowing_download` when the navigation became a download.
_DOWNLOAD_STARTED = object()


def _goto_allowing_download(page, url: str, **kw):
    """``page.goto`` that survives a navigation that stops being a navigation.

    An attachment navigation raises "Download is starting" and never loads: that one is reported
    as ``_DOWNLOAD_STARTED`` so the caller knows to wait for the file. One aborted outright
    (``net::ERR_ABORTED``, what doi.org does when it hands back the file) is a plain ``None`` —
    whatever the page shows is the answer. A navigation that never gets an answer at all is a
    ``None`` too: the endpoint accepted the request, so it is still worth one direct request.
    """
    try:
        return page.goto(url, **kw)
    except PlaywrightTimeoutError:
        return None
    except PlaywrightError as e:
        message = str(e)
        if "Download is starting" in message:
            return _DOWNLOAD_STARTED
        if "ERR_ABORTED" in message:
            return None
        # A challenge under this address reloads its own document; that is the gate being
        # worked on, and the request context still gets the file once the wait is over.
        if "interrupted by another navigation" in message:
            return None
        raise


#: A managed challenge replaces itself in about ten seconds here; give it that and more, up to
#: the navigation timeout -- a challenge answered with a re-navigation before it finishes is
#: answered one load too early.
_CHALLENGE_WAIT = 30.0
_CHALLENGE_POLL = 0.5


def _wait_replaced(control_port: int, profile, timeout: float = 8.0) -> None:
    """Wait for a broker that was told to leave, and the browser it was taking down with it.

    The shutdown reply comes before the teardown runs, and the teardown's SIGTERM needs a
    moment: asking for the next broker at once would meet a browser still holding the profile.
    A browser that stays (one the user started) answers on its port, and that ends the wait.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if browser_broker.live_port(profile) is not None:
            return
        try:
            socket.create_connection(("127.0.0.1", control_port), timeout=0.2).close()
        except OSError:
            if browser_broker.profile_holder(profile) is None:
                return
        time.sleep(0.05)


def _others_on(control_port: int) -> int:
    """How many leases the broker serves besides ours; 0 when it cannot say (so: replace)."""
    with contextlib.suppress(browser_broker.BrokerUnavailable, OSError, ValueError, KeyError,
                             TypeError):
        return int(browser_broker.status(control_port)["leases"]) - 1
    return 0


class Session:
    """A client of the browser broker: it holds a lease, and the browser is the broker's.

    Cheap to construct; :meth:`start` is the expensive part (it may start a broker). One session
    never drives another's tab: every session opens its own page and parks only that one.
    """

    def __init__(self, profile: str | None = None, headless: bool = False,
                 channel: str | None = None, proxy: str | None = None,
                 profile_directory: str = "Default", profile_source: str = "default",
                 pinned: bool = False, lease_factory=None, idle_minutes: float = 10.0):
        self._profile = paths.resolve_browser_profile(profile)
        self._profile_directory = profile_directory
        self._profile_source = profile_source
        self._headless = headless
        self._channel = channel
        self._proxy = proxy
        self._pinned = pinned
        self._idle_minutes = idle_minutes
        self._lease_factory = lease_factory
        self._user_profile = False  # computed per lease: the warm-up only applies to theirs
        self._lease: browser_broker.Lease | None = None
        self._runtime: _Runtime | None = None
        self._page_: object | None = None

    @property
    def mode(self) -> str:
        """``attached`` (someone else's browser), ``launched`` (ours), or ``none``."""
        if self._lease is None:
            return "none"
        return "attached" if not self._lease.owns else "launched"

    # -- lifetime --------------------------------------------------------------

    def alive(self) -> bool:
        """Whether a browser session answers right now.

        With a lease the broker itself is asked. Without one the discovery file is probed
        through a throwaway lease, so a dead file (or a port nothing listens on) reads as dead
        without this session adopting anything.
        """
        if self._lease is not None:
            return self._lease.live()
        info = browser_broker.read_session()
        if not info:
            return False
        try:
            lease = browser_broker.connect(info["control_port"], headless=self._headless)
        except browser_broker.BrokerUnavailable:
            return False
        lease.close()
        return True

    def ensure(self) -> None:
        """Take a lease on the profile's broker, starting one when there is none."""
        lease = self._new_lease()
        if lease.version != browser_broker.PROTOCOL_VERSION:
            if _others_on(lease.control_port) == 0:
                # Nobody else is reading it: replace it rather than leave a broker this
                # version cannot talk to. The browser itself is found again by its port.
                with contextlib.suppress(browser_broker.BrokerUnavailable, OSError):
                    browser_broker.shutdown(lease.control_port, force=True)
                lease.close()
                _wait_replaced(lease.control_port, self._profile)
                lease = self._new_lease()
            if lease.version != browser_broker.PROTOCOL_VERSION:
                debug.warn(f"the browser broker answers protocol {lease.version}, this litcli "
                             f"speaks {browser_broker.PROTOCOL_VERSION}; run `litcli browser "
                             "stop` to replace it")
        self._lease = lease
        self._user_profile = (self._profile_source == "default"
                              or paths.same_profile(self._profile,
                                                    paths.user_browser_profile_dir()))

    def _new_lease(self) -> browser_broker.Lease:
        if self._lease_factory is not None:
            return self._lease_factory(profile=self._profile, headless=self._headless,
                                       profile_directory=self._profile_directory,
                                       source=self._profile_source, pinned=self._pinned,
                                       channel=self._channel, proxy=self._proxy,
                                       idle_minutes=self._idle_minutes)
        return browser_broker.ensure_lease(
            self._profile, headless=self._headless,
            profile_directory=self._profile_directory, source=self._profile_source,
            pinned=self._pinned, channel=self._channel, proxy=self._proxy,
            idle_minutes=self._idle_minutes)

    def start(self) -> None:
        """Make sure a browser session is up and bind this session's driver to it."""
        if self._runtime is not None:
            return
        self.ensure()
        assert self._lease is not None
        self._bind(self._lease.cdpport)

    def _bind(self, port: int) -> None:
        self._runtime = _Runtime(*_attach_engine(port), port=port)

    def _recover(self) -> None:
        """Drop a dead engine and bind a fresh one, so the caller can retry once."""
        self._drop_runtime()
        self.ensure()
        assert self._lease is not None
        self._bind(self._lease.cdpport)

    def _drop_runtime(self) -> None:
        """Release the lease and stop the engine; the browser itself belongs to the broker."""
        runtime, self._runtime = self._runtime, None
        self._page_ = None
        with contextlib.suppress(Exception):
            if runtime is not None:
                runtime.stop()
        if self._lease is not None:
            with contextlib.suppress(Exception):
                self._lease.close()
            self._lease = None

    def reset(self) -> None:
        """Park this session's page on ``about:blank``.

        An answered file left up in the tab keeps a ``--pdf-renderer`` spinning after the driver
        has exited, and its signed address expires in minutes: the desktop then shows a dead
        link, which reads as a hung command. Another session's tab is left alone.
        """
        if self._lease is None and self._page_ is None:
            return  # nothing was started by this session: do not start one just to park it
        try:
            page = self._page_ if self._page_ is not None else self._page()
        except BrowserUnavailable:
            return
        with contextlib.suppress(Exception):
            page.goto("about:blank")

    def close(self) -> None:
        """Release the lease; the broker keeps the browser for the next command."""
        lease, self._lease = self._lease, None
        if lease is not None:
            with contextlib.suppress(Exception):
                lease.close()
        page, self._page_ = self._page_, None
        if page is not None:
            with contextlib.suppress(Exception):
                page.close()
        runtime, self._runtime = self._runtime, None
        if runtime is not None:
            with contextlib.suppress(Exception):
                runtime.stop()

    def terminate(self, force: bool = True) -> None:
        """Release the lease and ask the broker to stop.

        ``force`` stops it even while another lease is held; without it the broker stays for
        whoever still holds one. What happens to the browser is the broker's rule (its own and
        headless is killed, an attached one is kept).
        """
        port = self._lease.control_port if self._lease is not None else None
        self.close()
        if port is None:
            return
        with contextlib.suppress(browser_broker.BrokerUnavailable, OSError):
            browser_broker.shutdown(port, force=force)
        if force:
            # A forced stop takes the browser with it; the caller usually starts a new one at
            # once, and it must not arrive while the old one still holds the profile.
            _wait_replaced(port, self._profile)

    def alive_elsewhere(self) -> bool:
        """Whether some other process currently holds a lease on the broker."""
        info = browser_broker.read_session()
        if not info:
            return False
        try:
            lease = browser_broker.connect(info["control_port"], headless=self._headless)
        except browser_broker.BrokerUnavailable:
            return False
        lease.close()
        return True

    # -- work ------------------------------------------------------------------

    def _page(self):
        """This session's own tab, opened once and never somebody else's."""
        if self._runtime is None:
            self.start()
        assert self._runtime is not None
        if self._page_ is None:
            self._page_ = self._runtime.context.new_page()
        return self._page_

    def _retrying(self, work):
        """Run ``work``; a browser that went away is rebound and the work tried once more."""
        try:
            return work()
        except PlaywrightError as e:
            if not _went_away(e):
                raise
        self._recover()
        try:
            return work()
        except PlaywrightError as e:
            raise BrowserUnavailable(f"the browser session went away: {e}") from e

    def cookies(self, url: str) -> list[dict]:
        """The cookies the browser holds for ``url`` -- a signed-in visit's evidence."""
        if self._runtime is None:
            return []
        return self._runtime.context.cookies([url])

    def page(self, url: str) -> PageResponse:
        """Navigate and hand back the answer (one rebind-and-retry when the browser left).

        On the user's own profile a challenge that survives the retry gets warm-up cycles: the
        site root, then the address again. The challenge rides the signed-in chain, so the
        root's own exchange sets the clearance the article then loads with.
        """
        return self._retrying(lambda: self._navigate_warm(url))

    #: Warm-up cycles a challenge on the user's profile gets. The clearance the root sets lands
    #: on the load after it, so a single cycle can come back challenged; a second is what a
    #: bare re-run of the command would have given it, without the caller having to know.
    _WARM_CYCLES = 2

    def _navigate_warm(self, url: str) -> PageResponse:
        answer = self._navigate(url)
        page = self._page()
        for _ in range(self._WARM_CYCLES):
            if not (self._user_profile and is_challenge_page(answer.text)):
                break
            try:
                self._goto(page, _site_root(url))
                self._settle(page)
            except PlaywrightError:
                pass  # the challenge reloads its own document over the warm-up; the address tries
            answer = self._navigate(url)
        return answer

    def _navigate(self, url: str) -> PageResponse:
        """Navigate and hand back the answer (a gate page included; it is not an error here).

        A challenge that solves itself is waited out first: the first paint is the
        interstitial, not the answer -- it is replaced by the page on its own a few seconds
        later, and judging the first paint makes every gated host read as unusable while it is
        one automatic reload away from the article. A challenge still up after the wait earns
        one more navigation: the clearance the exchange sets belongs to the next load.
        """
        page = self._page()
        before = page.url  # a shared tab: an address that does not move is the last item's
        response = self._goto(page, url)
        text = self._settle(page)
        if is_challenge_page(text):
            try:
                response = self._goto(page, url) or response
                text = self._settle(page)
            except PlaywrightError:
                pass  # keep the gated answer already in hand
        # The redirect target, not the address asked for: the route may have become the file.
        # A navigation that never answered leaves the tab on the previous item's address --
        # that address is where the text in hand came from, so the caller's own url is the
        # honest answer once the tab has not moved.
        landed = page.url or (response.url if response is not None else url)
        if response is None and page.url == before:
            landed = url
        status = response.status if response is not None else 403
        if looks_like_pdf(landed) or (text and not is_challenge_page(text)):
            # The solved document where the interstitial answered, or the file itself: both are
            # the answer, and a viewer's shell is not an error page.
            status = 200
        return PageResponse(status=status, text=text, url=landed,
                            headers=dict(response.headers) if response is not None else {})

    def _goto(self, page, url: str):
        """Navigate, waiting only for the DOM.

        A managed challenge keeps a subresource open for longer than the navigation timeout, so
        `load` never fires while the page is already usable; waiting for `load` reports that
        host as one that never answered. A timeout is not fatal here either: the document may
        still have arrived, and the settle loop reads whatever is there.
        """
        try:
            return page.goto(url, wait_until="domcontentloaded")
        except PlaywrightTimeoutError:
            return None
        except PlaywrightError as e:
            # A challenge reloads its own document over the navigation; that is the exchange
            # in progress, and the settle loop reads whatever document is in hand after it.
            if "ERR_ABORTED" not in str(e) and "interrupted by another navigation" not in str(e):
                raise
            return None

    def _settle(self, page) -> str:
        """Read the document until the challenge is gone, or the wait runs out.

        Mid-exchange the document is being replaced and ``content()`` raises "the page is
        navigating and changing the content"; an unreadable page is the challenge still in
        progress, not an answer, so it is read again rather than escaped.
        """
        text = ""
        deadline = time.monotonic() + _CHALLENGE_WAIT
        while True:
            try:
                text = page.content()
                challenged = is_challenge_page(text)
            except PlaywrightError:
                challenged = True
            if not challenged or time.monotonic() >= deadline:
                return text
            time.sleep(_CHALLENGE_POLL)

    def download(self, url: str) -> bytes:
        """Navigate to a file URL and return its bytes (one rebind-and-retry when it left)."""
        try:
            return self._retrying(lambda: self._fetch(url))
        except PlaywrightError as e:
            # Closed mid-download: a failed copy, not a crash of the whole command.
            raise Unavailable(f"the session browser failed during the download: {e}") from e

    def _fetch(self, url: str) -> bytes:
        """Navigate to a file URL and return its bytes.

        Whether the endpoint is still working, refused for want of entitlement, or rendering the
        file inline is written in the page, not in elapsed time: a challenge keeps it coming and
        is waited out, any other document is already the answer. An answer that is not a download
        — Chrome's inline viewer, a redirect back to `/abs/`, a sign-in page — is re-requested
        through the session's own request context, which hands back the bytes rather than the
        viewer's HTML; the caller sorts the PDF from the refusal.
        """
        page = self._page()
        # The tab is shared by every item of a run, so an address that has not moved is the
        # *previous* item's document: a navigation that died without a word leaves it up, and
        # reading it as the answer hands the previous file back for this one.
        before = page.url
        caught: list = []

        def collect(download):  # a plain function: Playwright tags handlers with an attribute
            caught.append(download)

        # The navigation takes its timeout from the context default, so it is raised here and put
        # back (the other waits of the session keep their 30 s). The artifact itself has no
        # timeout of its own and is awaited until it lands: a slow transfer is a wait, not a no.
        context = self._runtime.context
        try:
            # Inside the try: a browser closed between the page and here fails this copy, not
            # the walk.
            context.set_default_timeout(_DOWNLOAD_WAIT * 1000)
            page.on("download", collect)
            #: The whole item runs against one clock, so no stage can spend the next one's. The
            #: navigation takes its own share of it, and at most the clock whole (a test pins
            #: the clock lower than the share).
            deadline = time.monotonic() + _DOWNLOAD_WAIT
            response = _goto_allowing_download(page, url, wait_until="commit",
                                               timeout=min(_NAV_WAIT, _DOWNLOAD_WAIT) * 1000)
            settled = time.monotonic() + _REDIRECT_GRACE
            while not caught:
                # A started download leaves the previous document up, which says nothing, so it
                # is the one case that keeps waiting without looking at the page.
                if response is not _DOWNLOAD_STARTED:
                    landed = page.url
                    # An answered navigation -- a response, whatever it says -- put this
                    # item's document in the tab even when the address string is the same as
                    # it was. Only a navigation that never answered leaves the previous one.
                    fresh = landed != before or response is not None
                    if looks_like_pdf(landed) and fresh:
                        break  # the file itself is up, where it was sent to
                    # A route can answer with the shell of its own viewer and redirect to the
                    # file a moment later (`/am/pii/…` → a signed CDN address). Told apart by
                    # the address having moved: a shell still under the route is a wait, a
                    # document at another address is the answer (`/abs/`, a sign-in page).
                    coming = _still_coming(page)
                    if landed != url and fresh and not coming:
                        break
                    if not coming and time.monotonic() >= settled:
                        break  # the route kept its shell: take it, it is not going to move
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                page.wait_for_timeout(min(_CHALLENGE_POLL, left) * 1000)
            if caught:
                saved = caught[0].path  # a str already on the offline stand-in
                return Path(saved() if callable(saved) else saved).read_bytes()
            # The bytes come from the address the navigation landed on: the route it started at
            # answers a bare request with its bot check, the file's own address does not.
            landed = page.url
            fresh = landed != before or response is not None
            target = landed if fresh and looks_like_pdf(landed) else url
            # The request context ignores the setting above and keeps its own 30 s, so its
            # timeout is named here: the clock's remainder, but never less than what the
            # navigation itself was given — by this point the loop may have spent it all.
            answer = context.request.get(
                target, headers={"Accept": "application/pdf"},
                timeout=max(_FALLBACK_WAIT, deadline - time.monotonic()) * 1000)
            return answer.body()
        finally:
            with contextlib.suppress(PlaywrightError):
                context.set_default_timeout(None)  # back to the library default
                page.remove_listener("download", collect)
