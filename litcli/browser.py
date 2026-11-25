"""One long-lived, real Chrome the entitled readers fetch through.

Copying clearance cookies by hand is what this replaces: a Cloudflare/WAF clearance is bound
to the user agent *and* the egress IP, and it expires in minutes, so every copy is a race.
A browser that is already signed in keeps its own session, renews it, and passes the gates by
being a browser — ScienceDirect rejects even headless Chromium (verified 2026-11-24), so the
page itself is fetched through here and the cookies are never lifted into ``httpx``.

The instance is shared by every site and by every command: :meth:`Session.start` reuses a
running one — it is found through a discovery file, or through ``/proc`` when that file is
gone — and :meth:`Session.terminate` stops the one a command started.
``launcher`` is injected so the whole module is testable without a browser.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from . import paths
from .backends._cf_gate import is_challenge_page
from .debug import notice
from .errors import External, Unavailable

#: How long a freshly spawned Chrome gets to open its debugging port.
_READY_TIMEOUT = 20.0
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


def looks_like_pdf(url: str) -> bool:
    """True when ``url`` is a file's own address.

    After a navigation lands on a PDF, Chrome keeps that file's address in the page URL: it is
    how a route that turned into the file is told from one that bounced back to a page (``/abs/``,
    a sign-in page), and where the bytes then come from.
    """
    return urlparse(str(url or "")).path.lower().endswith(".pdf")


#: The ceiling on waiting for a PDF endpoint. A clock cannot tell "no entitlement" from "still
#: downloading" — the document can, so the wait ends the moment the page is a document rather
#: than a challenge; this only bounds a challenge that never resolves, and the transfer after it.
_DOWNLOAD_WAIT = 300.0

#: How long a viewer shell under the route it was asked for is given to redirect into the file
#: (~2 s in practice; a bot check in between takes longer). Past it the shell is taken as the
#: answer, so an in-place HTML refusal does not sit out the whole ceiling above.
_REDIRECT_GRACE = 30.0

try:  # optional dependency: the module stays importable without Playwright
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
except ImportError:  # pragma: no cover - exercised through the missing-Playwright test
    class PlaywrightTimeoutError(Exception):  # type: ignore[no-redef]
        """Stand-in so :meth:`Session.download` reads the same either way."""

    PlaywrightError = OSError  # type: ignore[misc]


class BrowserUnavailable(External):
    """No usable browser: Playwright missing, the browser not downloaded, or nothing running."""

    exit_code = 2


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
    """The live objects a launcher returns, so a caller can close both the context and the driver.

    ``port``/``pid`` say how the browser can be found again once this process is gone: a
    detached browser is reachable over CDP at ``port`` and is its own ``pid``. A paired-down
    runtime (the bundled-browser path) has neither, and stays same-process only.
    """

    def __init__(self, context, stopper=None, port=None, pid=None):
        self.context = context
        self._stopper = stopper
        self.port = port
        self.pid = pid

    def stop(self) -> None:
        try:
            if not self.port:  # attached: closing the context would close the shared browser
                self.context.close()
        finally:
            if self._stopper:
                self._stopper()


def _running_on(profile: Path, root: Path = Path("/proc")) -> list[tuple[int, int | None]]:
    """``(pid, debugging port)`` for every browser already started on this profile.

    Process rows only — a zygote or renderer carries the same command line and would otherwise
    be reaped on its own (it goes with the browser that forked it).

    Chrome keeps no list of itself, so this reads ``/proc``: a browser whose discovery file
    was lost (a crashed run, a cleaned ``/tmp``) is still there and still usable, and starting
    a second one per command is what fills the machine with them. Profiles carry across
    processes, so the ``user-data-dir`` argument is the whole match.

    The command line is read as a whole string, not split on NUL: Chrome rewrites its own
    argv into one space-joined line, so a per-argument match finds nothing at all.
    """
    marker = f"--user-data-dir={profile}"
    found = []
    try:
        entries = list(root.iterdir())
    except OSError:
        return found  # not Linux: no /proc, and nothing is found (a launch follows)
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            line = (entry / "cmdline").read_bytes().decode("utf-8", "replace")
        except OSError:
            continue  # it exited under us
        at = line.find(marker)
        after = line[at + len(marker):at + len(marker) + 1] if at >= 0 else ""
        if at < 0 or after not in (" ", "\0", ""):
            continue  # absent, or a profile that only shares a prefix with ours
        if "--type=" in line:
            continue  # a zygote, renderer or utility process: it goes with its browser
        # Only the browser process takes a debugging port; its zygotes and renderers do not,
        # which is what picks the one that can be talked to out of the group.
        port = re.search(r"--remote-debugging-port=(\d+)", line)
        found.append((int(entry.name), int(port.group(1)) if port else None))
    return sorted(found)


def _wait_gone(pid: int, timeout: float = 3.0) -> None:
    """Wait for ``pid`` to disappear, escalating to SIGKILL.

    A browser takes a moment to shut down, and the next command (or a test's temp-dir cleanup)
    arrives immediately: without the wait it can still hold the profile lock, or still be
    writing into a directory about to be removed.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.05)
    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGKILL)


def _is_headless(pid: int) -> bool:
    """Whether ``pid`` runs without a window (its command line says so)."""
    try:
        line = Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8", "replace")
    except OSError:
        return False
    return "--headless" in line


def _free_port() -> int:
    """A port free right now (Chrome fails to start on a taken one)."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def cdp_ready(port: int, timeout: float = 0.5) -> bool:
    """Whether a browser answers CDP on ``port`` (the liveness probe for a detached session)."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{int(port)}/json/version",
                                    timeout=timeout) as answer:
            return answer.status == 200
    except (OSError, ValueError):
        return False


def _attach_engine(port: int):
    """A context of the already-running browser at ``port``, and the stop that detaches from it."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise BrowserUnavailable(
            "Playwright is not installed. Install it with `litcli backends install-browser` "
            "and retry (or fall back to a configured cookie + user_agent).") from e
    paths.apply_browser_cache()
    engine = sync_playwright().start()
    try:
        browser = engine.chromium.connect_over_cdp(f"http://127.0.0.1:{int(port)}")
    except Exception as e:
        engine.stop()
        raise BrowserUnavailable(
            f"could not attach to the running browser on port {port}: {e}") from e
    if not browser.contexts:
        engine.stop()
        raise BrowserUnavailable("the running browser has no window to work in")
    return browser.contexts[0], engine.stop


def _launch_detached(binary: str, profile, headless: bool, proxy: str | None) -> _Runtime:
    """Start a Chrome that outlives this process, then attach to it.

    Detached on purpose (``start_new_session``): `browser login` exits and the clearance must
    still be there for the next command, which attaches over CDP from a process of its own.
    """
    port = _free_port()
    profile = Path(profile)
    profile.mkdir(parents=True, exist_ok=True)
    argv = [binary, f"--user-data-dir={profile}", f"--remote-debugging-port={port}",
            "--no-first-run", "--no-default-browser-check", "about:blank"]
    if headless:
        argv.insert(1, "--headless=new")
    if proxy:
        # The clearance is bound to the egress IP, so the browser must leave by the same proxy
        # as httpx or every cookie it earned is worthless to the request path.
        argv.insert(1, f"--proxy-server={proxy}")
    try:
        child = subprocess.Popen(argv, start_new_session=True, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
    except OSError as e:
        raise BrowserUnavailable(f"could not start {binary}: {e}",
                                 hint="litcli backends install-browser") from e
    deadline = time.monotonic() + _READY_TIMEOUT
    while time.monotonic() < deadline and not cdp_ready(port):
        if child.poll() is not None:
            raise BrowserUnavailable(
                f"{binary} exited at once (code {child.returncode}); a headed browser needs a "
                "display", hint="litcli backends install-browser")
        time.sleep(0.1)
    if not cdp_ready(port):
        raise BrowserUnavailable(
            f"{binary} never opened its debugging port (pid {child.pid})",
            hint="litcli backends install-browser")
    context, stop = _attach_engine(port)
    return _Runtime(context, stop, port=port, pid=child.pid)


def _default_launcher(profile, headless, channel, proxy) -> _Runtime:
    """Start a browser against ``profile``.

    A real Chrome on the machine is started detached and driven over CDP, because the whole
    point is a session that outlives the command that signed in. Without one the bundled
    Chromium is launched in-process (``channel`` is dropped by :func:`session_from_config` in
    that case), which still works but only within the one command.
    """
    binary = next((found for name in _CHROME_BINARIES if (found := shutil.which(name))), None)
    if binary:
        try:
            return _launch_detached(binary, profile, headless, proxy)
        except BrowserUnavailable as e:
            # A real Chrome that cannot run (no display, say) must not sink the session: the
            # bundled browser serves this one command instead of nothing.
            notice(f"{e}; falling back to the bundled browser for this command")
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
        kw["proxy"] = {"server": proxy}
    try:
        context = engine.chromium.launch_persistent_context(str(profile), **kw)
    except Exception as e:
        engine.stop()
        raise BrowserUnavailable(
            f"could not start the browser (run `litcli backends install-browser` first): {e}",
            hint="litcli backends install-browser") from e
    return _Runtime(context, engine.stop)


#: How a real Chrome is spelled on each platform (the ``chrome`` channel needs one installed).
_CHROME_BINARIES = ("google-chrome", "google-chrome-stable", "chrome", "chromium", "chromium-browser")


def session_from_config(cfg: dict) -> Session | None:
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
        notice("no Chrome found; the browser session falls back to the bundled Chromium")
        channel = None
    return Session(profile=section.get("profile") or None,
                   headless=bool(section.get("headless", False)), channel=channel,
                   proxy=(cfg.get("network") or {}).get("proxy"))


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
        raise


#: A managed challenge replaces itself in about ten seconds here; give it that and more, up to
#: the navigation timeout -- a challenge answered with a re-navigation before it finishes is
#: answered one load too early.
_CHALLENGE_WAIT = 30.0
_CHALLENGE_POLL = 0.5


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
        """True while the discovery file names a browser that still answers.

        The pid alone is not enough: a browser can be closed while the file survives, and the
        pid may then even be reused. When the file names a debugging port, the browser has to
        answer on it.
        """
        try:
            info = json.loads(paths.browser_session_file().read_text(encoding="utf-8"))
            os.kill(int(info["pid"]), 0)
        except (OSError, ValueError, KeyError, TypeError):
            return False
        port = info.get("port")
        return cdp_ready(port) if port else True

    def start(self) -> None:
        """Reuse the browser already on this profile; launch one only when there is none.

        The shared profile exists so a sign-in survives; the same reuse is what keeps one
        browser in the machine instead of one per command (each is ~10 processes). A browser
        found on the profile but missing from the discovery file is put back in it rather than
        followed by a second one, and anything else left over on the profile is stopped.
        """
        if self.alive():
            return
        self._profile.mkdir(parents=True, exist_ok=True)
        reachable = [(pid, port) for pid, port in _running_on(self._profile) if port]
        if reachable:  # running, but the file was lost or names something else
            pid, port = reachable[0]
            if self._headless or not _is_headless(pid):
                self._record(pid, port)
                self._reap(keep=pid)
                return
            # A sign-in needs a window and a headless browser has none: handing one back to
            # `browser login` reads as the command having done nothing. The profile carries
            # the cookies across, so nothing is lost by replacing it.
            self._stop(pid)
            self._reap(keep=None)
        # A port-less browser holds the profile lock and can never be attached, so it goes
        # before the launch: a second browser on a locked profile just exits.
        self._reap(keep=None)
        try:
            self._runtime = self._launcher(self._profile, self._headless, self._channel,
                                           self._proxy)
        except BrowserUnavailable:
            # One can still appear between the scan and the launch (two commands at once); it
            # is the session now, and losing that race is not a failed start.
            reachable = [(pid, port) for pid, port in _running_on(self._profile) if port]
            if not reachable:
                raise
            pid, port = reachable[0]
            self._record(pid, port)
            return
        self._owned = True
        pid = getattr(self._runtime, "pid", None)
        self._record(pid, getattr(self._runtime, "port", None))
        self._reap(keep=pid)  # a leftover too old to be in the file is still costing memory

    def _record(self, pid: int | None, port: int | None) -> None:
        record = {"profile": str(self._profile), "pid": pid or os.getpid()}
        if port:  # another process finds the browser through this
            record["port"] = int(port)
        paths.browser_session_file().parent.mkdir(parents=True, exist_ok=True)
        paths.browser_session_file().write_text(json.dumps(record), encoding="utf-8")

    def _reap(self, keep: int | None) -> None:
        """Stop the other browsers on this profile: nothing attaches to them again."""
        for pid, _port in _running_on(self._profile):
            if pid == keep:
                continue
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGTERM)

    def reset(self) -> None:
        """Park the tabs on ``about:blank`` and drop the extras.

        An answered file left up in the tab keeps a ``--pdf-renderer`` spinning after the driver
        has exited, and its signed address expires in minutes: the desktop then shows a dead
        link, which reads as a hung command. The browser itself is left running (``close`` is
        what detaches it); only the page content is cleared.
        """
        if not self.alive():
            return
        try:
            if self._runtime is None:
                self._attach()
            pages = self._runtime.context.pages
            for page in pages:
                with contextlib.suppress(Exception):
                    page.goto("about:blank")
            for page in pages[1:]:
                with contextlib.suppress(Exception):
                    page.close()
        except Exception:  # a browser that died mid-way is not worth failing the command for
            pass

    def close(self) -> None:
        """Disconnect; a detached browser keeps running for the next command."""
        if self._runtime:
            self._runtime.stop()
        self._runtime = None
        self._owned = False
        if self.alive_elsewhere():
            return  # the browser is its own process: the file still describes a real session
        paths.browser_session_file().unlink(missing_ok=True)

    def terminate(self, force: bool = False) -> None:
        """Disconnect, and stop the browser if this process started it.

        ``close`` deliberately leaves a detached browser up so the clearance survives to the
        next command; a command that runs on its own (a survey walk) has no next command and
        would otherwise leave one browser behind per run. ``force`` stops the browser in the
        discovery file even when another process started it (a browser left by a helper
        subprocess, say) — a sign-in the user opened up by hand is the case it does not cover.
        """
        pid = None
        if force:
            pid = self._named_pid()
        elif self._owned and self._runtime:
            pid = self._runtime.pid
        self.close()
        if pid is None:
            return
        self._stop(pid)
        if self._named_pid() == pid:
            paths.browser_session_file().unlink(missing_ok=True)

    def _stop(self, pid: int) -> None:
        """SIGTERM ``pid`` and wait for it to go."""
        with contextlib.suppress(OSError, ProcessLookupError):
            os.kill(pid, signal.SIGTERM)
        _wait_gone(pid)

    def _named_pid(self) -> int | None:
        """The browser the discovery file names, if there is a readable one."""
        try:
            return int(json.loads(paths.browser_session_file().read_text(encoding="utf-8"))["pid"])
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def alive_elsewhere(self) -> bool:
        """Alive and owned by a process other than this one."""
        try:
            info = json.loads(paths.browser_session_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return int(info.get("pid", 0)) != os.getpid()

    def _attach(self) -> None:
        """Attach to the browser in the discovery file (one started by another job or command)."""
        try:
            port = int(json.loads(paths.browser_session_file().read_text(encoding="utf-8"))["port"])
        except (OSError, ValueError, KeyError, TypeError) as e:
            raise BrowserUnavailable(
                "the browser session belongs to another process and is not attachable "
                "(it was started without a debugging port)", hint="litcli browser login") from e
        self._runtime = _Runtime(*_attach_engine(port), port=port)

    # -- work ------------------------------------------------------------------

    def _page(self):
        if not self.alive():
            raise BrowserUnavailable(
                "no browser session is running — run `litcli browser login <site>` first "
                "(or configure a cookie + user_agent to use the HTTP path).",
                hint="litcli browser login")
        if self._runtime is None:  # started by another process: attach, then reuse it
            self._attach()
        page = self._runtime.context.pages[0] if self._runtime.context.pages \
            else self._runtime.context.new_page()
        return page

    def page(self, url: str) -> PageResponse:
        """Navigate and hand back the answer (a gate page included; it is not an error here).

        A challenge that solves itself is waited out first: the first paint is the
        interstitial, not the answer -- it is replaced by the page on its own a few seconds
        later, and judging the first paint makes every gated host read as unusable while it is
        one automatic reload away from the article. A challenge still up after the wait earns
        one more navigation: the clearance the exchange sets belongs to the next load.
        """
        page = self._page()
        response = self._goto(page, url)
        text = self._settle(page)
        if is_challenge_page(text):
            try:
                response = self._goto(page, url) or response
                text = self._settle(page)
            except PlaywrightError:
                pass  # keep the gated answer already in hand
        # The redirect target, not the address asked for: the route may have become the file.
        landed = page.url or (response.url if response is not None else url)
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
            if "ERR_ABORTED" not in str(e):
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
        """Navigate to a file URL and return its bytes.

        Whether the endpoint is still working, refused for want of entitlement, or rendering the
        file inline is written in the page, not in elapsed time: a challenge keeps it coming and
        is waited out, any other document is already the answer. An answer that is not a download
        — Chrome's inline viewer, a redirect back to `/abs/`, a sign-in page — is re-requested
        through the session's own request context, which hands back the bytes rather than the
        viewer's HTML; the caller sorts the PDF from the refusal.
        """
        page = self._page()
        caught: list = []

        def collect(download):  # a plain function: Playwright tags handlers with an attribute
            caught.append(download)

        # The navigation takes its timeout from the context default, so it is raised here and put
        # back (the other waits of the session keep their 30 s). The artifact itself has no
        # timeout of its own and is awaited until it lands: a slow transfer is a wait, not a no.
        context = self._runtime.context
        context.set_default_timeout(_DOWNLOAD_WAIT * 1000)
        try:
            page.on("download", collect)
            response = _goto_allowing_download(page, url, wait_until="commit")
            deadline = time.monotonic() + _DOWNLOAD_WAIT
            settled = time.monotonic() + _REDIRECT_GRACE
            while not caught:
                # A started download leaves the previous document up, which says nothing, so it
                # is the one case that keeps waiting without looking at the page.
                if response is not _DOWNLOAD_STARTED:
                    landed = page.url
                    if looks_like_pdf(landed):
                        break  # the file itself is up, where it was sent to
                    # A route can answer with the shell of its own viewer and redirect to the
                    # file a moment later (`/am/pii/…` → a signed CDN address). Told apart by
                    # the address having moved: a shell still under the route is a wait, a
                    # document at another address is the answer (`/abs/`, a sign-in page).
                    coming = _still_coming(page)
                    if landed != url and not coming:
                        break
                    if not coming and time.monotonic() >= settled:
                        break  # the route kept its shell: take it, it is not going to move
                if time.monotonic() >= deadline:
                    break
                page.wait_for_timeout(_CHALLENGE_POLL * 1000)
            if caught:
                saved = caught[0].path  # a str already on the offline stand-in
                return Path(saved() if callable(saved) else saved).read_bytes()
            # The bytes come from the address the navigation landed on: the route it started at
            # answers a bare request with its bot check, the file's own address does not.
            target = page.url if looks_like_pdf(page.url) else url
            # The request context keeps its own 30 s regardless of the setting above.
            answer = context.request.get(target, headers={"Accept": "application/pdf"},
                                         timeout=_DOWNLOAD_WAIT * 1000)
            return answer.body()
        except PlaywrightError as e:
            # The window was closed mid-download (TargetClosedError) or the page went away:
            # a failed download, not a crash of the whole command.
            raise Unavailable(f"the session browser failed during the download: {e}") from e
        finally:
            context.set_default_timeout(None)  # back to the library default
            with contextlib.suppress(PlaywrightError):
                page.remove_listener("download", collect)
