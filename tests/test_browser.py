"""The browser session object: one running instance, discoverable, reusable.

The launcher is injected, so nothing here opens a browser.
"""
import functools
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import litcli.browser as browser
import litcli.paths as paths
from litcli.browser import Session
from tests.fake_browser import FakeDownload, FakeLease, FakeRuntime

#: A pid no process has: the runtime's browser was reaped under it.
DEAD_PID = 999999


def kill_unless_gone(pid, *_args):
    """``os.kill`` stand-in: the dead pid has gone, everything else still answers."""
    if int(pid) == DEAD_PID:
        raise ProcessLookupError(pid)


class FakeClock:
    """A monotonic clock a test moves by hand: nothing here waits in real time."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
#: A child process that starts the shared session and then exits, like `browser login` does.
CHILD = """
import sys
from litcli import paths
from litcli.browser import Session
Session(profile=paths.browser_profile_dir(), headless=True).start()
print("up", flush=True)
"""


class _SessionFixture(unittest.TestCase):
    """A session holding a fake lease; the driver stays out of it (``_attach_engine`` is stubbed).

    Nothing here starts a process: the lease is what a broker would answer, and the context is
    a :class:`FakeRuntime` whose pages serve a queue.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(paths, "_IS_WINDOWS", False)
        patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, {"XDG_DATA_HOME": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        paths.browser_profile_dir().mkdir(parents=True)
        self.lease = FakeLease()
        self.runtime = FakeRuntime()
        self.context = self.runtime
        self.stop = mock.Mock()
        self.factory_kwargs: dict = {}
        attach = mock.patch.object(browser, "_attach_engine",
                                   return_value=(self.context, self.stop))
        attach.start()
        self.addCleanup(attach.stop)
        self.s = Session(lease_factory=self.factory)

    def factory(self, **kw):
        self.factory_kwargs = kw
        return self.lease


class LeaseClientTest(_SessionFixture):
    """The session is a *client* of a broker: it holds a lease, never the browser itself."""

    def test_start_names_the_mode_from_ownership(self):
        self.s.start()
        self.assertEqual(self.s.mode, "attached")
        self.assertTrue(self.s.alive())

    def test_alive_is_false_once_the_lease_is_gone(self):
        self.s.start()
        self.lease.alive_flag = False
        self.assertFalse(self.s.alive())

    def test_each_session_gets_its_own_page_and_never_pages_zero(self):
        self.s.start()
        self.assertIs(self.s._page(), self.s._page())
        self.assertIsNot(self.s._page(), self.s._runtime.context.pages[0])

    def test_close_releases_the_lease_before_the_page(self):
        order = []
        self.s.start()
        self.s._page()
        self.s._lease.close = lambda: order.append("lease")
        self.s._page_.close = lambda: order.append("page")
        self.s.close()
        self.assertEqual(order[:2], ["lease", "page"])

    def test_terminate_asks_the_broker_to_stop(self):
        self.s.start()
        with mock.patch.object(browser.browser_broker, "shutdown") as down:
            self.s.terminate()
        down.assert_called_once_with(self.lease.control_port, force=True)

    def test_reset_parks_only_its_own_page(self):
        self.s.start()
        own, first = self.s._page(), self.s._runtime.context.pages[0]
        self.s.reset()
        own.goto.assert_called_with("about:blank")
        first.goto.assert_not_called()

    def test_a_dead_engine_rebinds_and_retries_once(self):
        self.s.start()
        with mock.patch.object(self.s, "_goto",
                               side_effect=[browser.PlaywrightError("Target closed"), None]), \
                mock.patch.object(browser, "_DOWNLOAD_WAIT", 0.05):
            self.s.page("https://example.org/a")
        self.assertEqual(browser._attach_engine.call_count, 2)
        self.stop.assert_called_once()  # the dead engine goes before the new one is bound


class SessionTest(_SessionFixture):
    def test_a_second_start_reuses_the_running_one(self):
        self.s.start()
        self.s.start()
        self.assertEqual(browser._attach_engine.call_count, 1)

    def test_the_mode_is_launched_for_a_broker_this_session_started(self):
        self.lease.owns = True
        self.s.start()
        self.assertEqual(self.s.mode, "launched")

    def test_no_lease_and_no_broker_is_mode_none(self):
        self.assertEqual(self.s.mode, "none")

    def test_alive_probes_the_broker_without_holding_it(self):
        paths.browser_session_file().write_text(json.dumps(
            {"pid": os.getpid(), "control_port": 4321, "cdp_port": 41_000}))
        with mock.patch.object(browser.browser_broker, "connect") as conn:
            self.assertTrue(self.s.alive())
        conn.return_value.close.assert_called_once()

    def test_alive_is_false_when_the_probe_fails(self):
        paths.browser_session_file().write_text(json.dumps(
            {"pid": os.getpid(), "control_port": 4321, "cdp_port": 41_000}))
        gone = browser.browser_broker.BrokerUnavailable("no")
        with mock.patch.object(browser.browser_broker, "connect", side_effect=gone):
            self.assertFalse(self.s.alive())

    def test_the_lease_carries_the_profile_it_was_configured_with(self):
        self.s = Session(profile="dedicated", profile_directory="Profile 1",
                         profile_source="config", pinned=True, lease_factory=self.factory)
        self.s.start()
        self.assertEqual(self.factory_kwargs["profile"], paths.browser_profile_dir())
        self.assertEqual(self.factory_kwargs["profile_directory"], "Profile 1")
        self.assertEqual(self.factory_kwargs["source"], "config")
        self.assertTrue(self.factory_kwargs["pinned"])

    def test_a_lease_from_an_older_broker_is_replaced(self):
        stale = FakeLease(owns=False)
        stale.version = 0
        leases = iter([stale, self.lease])

        def factory(**_kw):
            return next(leases)

        self.s = Session(lease_factory=factory)
        with mock.patch.object(browser.browser_broker, "shutdown") as down:
            self.s.start()
        down.assert_called_once_with(stale.control_port, force=True)
        self.assertEqual(self.s._lease.version, browser.browser_broker.PROTOCOL_VERSION)

    def test_an_older_broker_this_session_owns_only_warns(self):
        self.lease.version = 0
        self.lease.owns = True
        with mock.patch.object(browser.debug, "notice") as told, \
                mock.patch.object(browser.browser_broker, "shutdown") as down:
            self.s.start()
        down.assert_not_called()
        told.assert_called_once()

    def test_reset_without_a_browser_is_quiet(self):
        self.s.reset()

    def test_proxy_reaches_the_lease_factory(self):
        Session(proxy="http://127.0.0.1:7890", lease_factory=self.factory).start()
        self.assertEqual(self.factory_kwargs["proxy"], "http://127.0.0.1:7890")

    def test_close_when_never_started_is_quiet(self):
        self.s.close()

    def test_terminate_when_never_started_still_asks_the_broker(self):
        with mock.patch.object(browser.browser_broker, "shutdown") as down:
            self.s.terminate()
        down.assert_not_called()

    def test_a_challenge_that_solves_is_waited_out(self):
        # A managed challenge paints "Just a moment..." first and replaces itself ~10 s later;
        # judged on the first paint every gated site reads as unusable while it is a direct
        # load away from the article.
        self.s.start()
        self.runtime.queue.append((403, "<html><title>Just a moment...</title></html>",
                                    "<html><title>APS Journals</title></html>"))
        answer = self.s.page("https://link.aps.org/")
        self.assertEqual((answer.status, "Just a moment" in answer.text), (200, False))

    def test_a_challenge_that_never_solves_keeps_its_gate(self):
        self.s.start()
        self.runtime.queue.append((403, "<html><title>Just a moment...</title></html>"))
        with mock.patch.object(browser, "_CHALLENGE_WAIT", 0):
            answer = self.s.page("https://link.aps.org/")
        self.assertEqual(answer.status, 403)

    def test_a_challenge_that_outlasts_the_wait_is_navigated_once_more(self):
        # The clearance cookie belongs to the exchange, so a host that paces it past the wait
        # clears on the next load rather than this one: one more navigation is a second chance
        # at the article, not a second guess.
        self.s.start()
        self.runtime.queue.extend([(403, "<html><title>Just a moment...</title></html>"),
                                    (200, "<html><title>APS Journals</title></html>")])
        with mock.patch.object(browser, "_CHALLENGE_WAIT", 0):
            answer = self.s.page("https://link.aps.org/")
        self.assertEqual((answer.status, "Just a moment" in answer.text), (200, False))

    def test_a_page_caught_mid_reload_is_read_again(self):
        # While the challenge swaps the document out, `content()` raises "the page is
        # navigating and changing the content"; that is the challenge in progress, not a dead
        # page, and it must not escape the wait.
        self.s.start()
        page = mock.MagicMock()
        page.goto.return_value = mock.Mock(status=403, headers={}, url="https://x/")
        page.content.side_effect = [
            browser.PlaywrightError("Page.content: Unable to retrieve content"),
            "<html><title>Just a moment...</title></html>",
            "<html><title>APS Journals</title></html>",
        ]
        with mock.patch.object(self.s, "_page", return_value=page), \
                mock.patch.object(browser, "_CHALLENGE_WAIT", 5), \
                mock.patch.object(browser, "_CHALLENGE_POLL", 0):
            answer = self.s.page("https://link.aps.org/")
        self.assertEqual((answer.status, "APS Journals" in answer.text), (200, True))

    def test_a_navigation_that_times_out_still_hands_back_the_page(self):
        # A managed challenge holds a subresource open past the navigation timeout, so `load`
        # never fires while the document is already usable: the timeout is no answer only if
        # nothing readable turns up afterwards.
        self.s.start()
        page = mock.MagicMock()
        page.goto.side_effect = [
            browser.PlaywrightTimeoutError("Page.goto: Timeout 30000ms exceeded."),
            mock.Mock(status=403, headers={}, url="https://x/"),
        ]
        page.content.side_effect = ["<html><title>Just a moment...</title></html>",
                                    "<html><title>AIAA</title></html>"]
        with mock.patch.object(self.s, "_page", return_value=page), \
                mock.patch.object(browser, "_CHALLENGE_WAIT", 0):
            answer = self.s.page("https://arc.aiaa.org/")
        self.assertEqual((answer.status, "AIAA" in answer.text), (200, True))

    def test_a_navigation_aborted_outright_still_hands_back_the_page(self):
        # doi.org answers some links with the file itself and Chrome aborts the navigation
        # (net::ERR_ABORTED); that is not a dead host, the page is the answer.
        self.s.start()
        page = mock.MagicMock()
        page.goto.side_effect = browser.PlaywrightError(
            "Page.goto: net::ERR_ABORTED at https://doi.org/10.1063/1.870872")
        page.content.return_value = "<html><title>AIP</title></html>"
        with mock.patch.object(self.s, "_page", return_value=page):
            answer = self.s.page("https://doi.org/10.1063/1.870872")
        self.assertEqual((answer.status, "AIP" in answer.text), (200, True))

    def test_a_page_navigation_that_never_answers_reports_the_requested_address(self):
        # Same shared tab as in ``download``: a navigation that dies silently leaves the
        # previous item's address in the tab, and reporting it makes the caller believe the
        # document in hand came from somewhere else.
        self.s.start()
        page = mock.MagicMock()
        page.url = "https://x/previous"
        page.goto.side_effect = browser.PlaywrightTimeoutError("Page.goto: Timeout")
        page.content.return_value = "<html><title>Previous</title></html>"
        with mock.patch.object(self.s, "_page", return_value=page), \
                mock.patch.object(browser, "_CHALLENGE_WAIT", 0):
            answer = self.s.page("https://x/current.pdf")
        self.assertEqual(answer.url, "https://x/current.pdf")

    def test_a_gate_page_is_returned_not_raised(self):
        self.s.start()
        self.runtime.queue.append((403, "<html>__cf_chl</html>"))
        with mock.patch.object(browser, "_CHALLENGE_WAIT", 0):
            self.assertEqual(self.s.page("https://x/a").status, 403)

    def test_a_download_returns_the_bytes(self):
        self.s.start()
        self.runtime.queue.append(b"%PDF-1.4 body")
        self.assertTrue(self.s.download("https://x/a.pdf").startswith(b"%PDF"))

    def test_a_pdf_gets_five_minutes_to_arrive(self):
        # The wait covers the challenge being solved and a slow link alike; four seconds cut
        # both off and sent the request context at the interstitial instead of the file.
        self.assertEqual(browser._DOWNLOAD_WAIT, 300.0)

    def test_the_navigation_and_the_transfer_share_one_clock(self):
        # Without a bound each of navigation, challenge wait and fallback request carried its
        # own full five minutes, so one dead item held the run for a quarter of an hour. The
        # navigation is now capped short of the whole, and the request gets only the rest.
        self.s.start()
        page = mock.MagicMock()
        page.url = "https://x/a.pdf"
        page.content.return_value = "<html>Just a moment...</html>"
        seen = {}
        clock = FakeClock()

        def goto(url, **kw):
            seen["goto"] = kw.get("timeout")
            clock.now += browser._NAV_WAIT  # the navigation spent its whole share
            return mock.Mock(status=200, headers={}, url=url)

        page.goto.side_effect = goto
        with mock.patch.object(self.s, "_page", return_value=page), \
                mock.patch.object(browser.time, "monotonic", clock), \
                mock.patch.object(browser, "_CHALLENGE_POLL", 0):
            self.s.download("https://x/a.pdf")
        (_url, kw), = self.s._runtime.context.request.calls
        self.assertEqual(seen["goto"], browser._NAV_WAIT * 1000)
        self.assertEqual(kw["timeout"], (browser._DOWNLOAD_WAIT - browser._NAV_WAIT) * 1000,
                         "what the navigation spent is gone from the transfer's budget")

    def test_a_challenge_that_never_resolves_is_cut_at_the_deadline(self):
        # The loop used to poll only until its own five minutes, on top of the navigation's:
        # a host that never solves had the run sit there. One deadline ends it, and the
        # fallback request still gets its minimum rather than a zero timeout.
        self.s.start()
        page = mock.MagicMock()
        page.url = "https://x/am/pii/S1"
        page.content.return_value = "<html>Just a moment...</html>"
        clock = FakeClock()

        def goto(url, **_kw):
            clock.now += 3.5  # the navigation took almost all of the four seconds
            return mock.Mock(status=403, headers={}, url=url)

        page.goto.side_effect = goto
        polls = []

        def wait(ms):
            polls.append(ms)
            clock.now += ms / 1000.0

        page.wait_for_timeout.side_effect = wait
        with mock.patch.object(self.s, "_page", return_value=page), \
                mock.patch.object(browser.time, "monotonic", clock), \
                mock.patch.object(browser, "_DOWNLOAD_WAIT", 4.0), \
                mock.patch.object(browser, "_CHALLENGE_POLL", 0.5):
            self.assertEqual(self.s.download("https://x/am/pii/S1"), b"%PDF inline")
        self.assertLessEqual(len(polls), 2, "the loop spent more than the navigation left")
        (_url, kw), = self.s._runtime.context.request.calls
        # The clock being spent must not cut the last-resort request to the leftover second:
        # it still gets the navigation's own budget.
        self.assertEqual(kw["timeout"], browser._FALLBACK_WAIT * 1000)

    def test_a_navigation_that_never_answers_does_not_reuse_the_last_file(self):
        # The tab is shared by every item of a run: when the navigation dies without a word,
        # ``page.url`` still shows the previous paper's PDF. Trusting it broke the wait at once
        # and re-requested the previous address, so the previous bytes were saved under the
        # current DOI.
        self.s.start()
        page = mock.MagicMock()
        page.url = "https://x/previous.pdf"
        page.goto.side_effect = browser.PlaywrightTimeoutError("Page.goto: Timeout")
        page.content.return_value = "%PDF-1.4 previous"
        with mock.patch.object(self.s, "_page", return_value=page), \
                mock.patch.object(browser, "_REDIRECT_GRACE", 0), \
                mock.patch.object(browser, "_CHALLENGE_POLL", 0):
            data = self.s.download("https://x/current.pdf")
        self.assertTrue(data.startswith(b"%PDF"))
        (url, _kw), = self.s._runtime.context.request.calls
        self.assertEqual(url, "https://x/current.pdf")

    def test_a_download_that_starts_late_is_still_taken(self):
        # A gated host serves the interstitial and only becomes the file once it is solved.
        self.s.start()
        page = mock.MagicMock()
        page.goto.return_value = mock.Mock(status=403, headers={}, url="https://x/a.pdf")
        state = {"solved": False, "polls": 0}
        handlers = {}
        page.on.side_effect = lambda name, fn: handlers.__setitem__(name, fn)
        page.content.side_effect = lambda: ("<html>PDF viewer</html>" if state["solved"]
                                            else "<html>Just a moment...</html>")

        def wait(_ms):
            state["polls"] += 1
            if state["polls"] == 3:
                state["solved"] = True
                handlers["download"](FakeDownload(b"%PDF-1.4 late"))

        page.wait_for_timeout.side_effect = wait
        with mock.patch.object(self.s, "_page", return_value=page), \
                mock.patch.object(browser, "_CHALLENGE_POLL", 0):
            data = self.s.download("https://x/a.pdf")
        self.assertTrue(data.startswith(b"%PDF"))
        self.assertEqual(state["polls"], 3)
        self.assertEqual(self.s._runtime.context.request.calls, [], "the file never arrived "
                         "as a download, so the fallback must not have run")

    def test_a_viewer_page_falls_back_without_burning_the_whole_wait(self):
        # An inline-rendered PDF never fires a download; waiting the full two minutes for one
        # would be dead time, so a document that is no longer a challenge means "read it now".
        self.s.start()
        page = mock.MagicMock()
        page.goto.return_value = mock.Mock(status=200, headers={}, url="https://x/a.pdf")
        page.content.return_value = '<html><embed type="application/pdf"></html>'
        page.url = "https://x/a.pdf"
        polls = []
        page.wait_for_timeout.side_effect = polls.append
        with mock.patch.object(self.s, "_page", return_value=page), \
                mock.patch.object(browser, "_CHALLENGE_POLL", 0):
            data = self.s.download("https://x/a.pdf")
        self.assertTrue(data.startswith(b"%PDF"))
        self.assertLess(len(polls), 5)
        # The fallback keeps the patience of the download it stands in for, and the session's
        # other waits get their default back.
        (_url, kw), = self.s._runtime.context.request.calls
        self.assertAlmostEqual(kw["timeout"], browser._DOWNLOAD_WAIT * 1000, delta=1000)
        self.assertIsNone(self.s._runtime.context.timeout)  # None == the library default

    def test_a_file_reached_by_redirect_is_read_from_where_it_landed(self):
        # An open-archive route serves the viewer shell of itself and only then redirects to
        # the file's own signed address; asking the request context for the route instead gets
        # the site's bot check, not the file, so the fallback has to use the landed address.
        self.s.start()
        page = mock.MagicMock()
        page.goto.return_value = mock.Mock(status=200, headers={},
                                          url="https://x/am/pii/S1")
        page.url = "https://x/am/pii/S1"
        page.content.return_value = "<html><body></body></html>"  # the viewer shell
        landed = "https://cdn.example/1-s2.0-S1/am.pdf?sig=1"

        def wait(_ms):
            page.url = landed
            page.content.return_value = "<html><body></body></html>"

        page.wait_for_timeout.side_effect = wait
        with mock.patch.object(self.s, "_page", return_value=page), \
                mock.patch.object(browser, "_CHALLENGE_POLL", 0):
            data = self.s.download("https://x/am/pii/S1")
        self.assertTrue(data.startswith(b"%PDF"))
        (url, _kw), = self.s._runtime.context.request.calls
        self.assertEqual(url, landed)

    def test_an_answer_in_place_is_not_waited_out_to_the_cap(self):
        # An unentitled endpoint may answer with a page at its own address instead of sending
        # the client anywhere; that document is the answer, and five minutes would be dead time.
        self.s.start()
        page = mock.MagicMock()
        page.goto.return_value = mock.Mock(status=200, headers={}, url="https://x/pii/S1/pdfft")
        page.url = "https://x/pii/S1/pdfft"
        page.content.return_value = "<html><title>ScienceDirect</title></html>"
        polls = []
        page.wait_for_timeout.side_effect = polls.append
        with mock.patch.object(self.s, "_page", return_value=page), \
                mock.patch.object(browser, "_CHALLENGE_POLL", 0), \
                mock.patch.object(browser, "_REDIRECT_GRACE", 0):
            self.s.download("https://x/pii/S1/pdfft")
        self.assertLess(len(polls), 5)

    def test_a_navigation_that_never_answers_falls_back_instead_of_failing(self):
        # An endpoint that accepts the request and then says nothing (a handle 302ing to a
        # repository) runs the navigation out. The file is still worth one direct request,
        # which is where a dead navigation used to leave the caller empty-handed.
        self.s.start()
        page = mock.MagicMock()
        page.goto.side_effect = browser.PlaywrightTimeoutError("Page.goto: Timeout 300000ms "
                                                              "exceeded.")
        page.url = "https://hdl.handle.net/11511/123"
        page.content.return_value = "<html>" + "x" * 50 + "</html>"
        with mock.patch.object(self.s, "_page", return_value=page), \
                mock.patch.object(browser, "_CHALLENGE_POLL", 0):
            data = self.s.download("https://hdl.handle.net/11511/123")
        self.assertTrue(data.startswith(b"%PDF"))

    def test_a_browser_dying_mid_download_is_an_unavailable(self):
        # A Chrome closed while the download is awaited raises TargetClosedError, which is a
        # Playwright error and not a timeout: unmapped it leaves the typed catches behind.
        self.s.start()
        page = mock.MagicMock()
        page.goto.return_value = mock.Mock(status=403, headers={}, url="https://x/a.pdf")
        page.content.return_value = "<html>Just a moment...</html>"
        page.wait_for_timeout.side_effect = browser.PlaywrightError(
            "Target page, context or browser has been closed")
        with mock.patch.object(self.s, "_page", return_value=page), \
                mock.patch.object(browser, "_CHALLENGE_POLL", 0), \
                self.assertRaises(browser.BrowserUnavailable):
            self.s.download("https://x/a.pdf")

    def test_a_dead_engine_with_nowhere_to_go_is_unavailable(self):
        # The retry is one attempt, not a loop: a browser that cannot come back is the copy's
        # failure, and the walk records it instead of unwinding the driver's own error.
        self.s.start()
        page = mock.MagicMock()
        page.goto.side_effect = browser.PlaywrightError("Target closed")
        with mock.patch.object(self.s, "_page", return_value=page), \
                mock.patch.object(browser, "_attach_engine",
                                  side_effect=browser.BrowserUnavailable("gone")), \
                self.assertRaises(browser.BrowserUnavailable):
            self.s.page("https://e.test/a")

    def test_a_stale_session_file_is_not_alive(self):
        paths.browser_session_file().write_text('{"pid": 999999, "profile": "/x"}')
        self.assertFalse(self.s.alive())

    def test_alive_is_false_once_closed(self):
        self.s.start()
        self.s.close()
        self.assertFalse(self.s.alive())


class _Handler(http.server.SimpleHTTPRequestHandler):
    """Serves ``*.pdf`` as an attachment (``*.inline.pdf`` renders in the viewer instead)."""

    def end_headers(self):
        if self.path.endswith(".pdf") and not self.path.endswith(".inline.pdf"):
            self.send_header("Content-Disposition", "attachment")
        super().end_headers()

    def log_message(self, *_args):
        pass


class SharedSessionTest(unittest.TestCase):
    """The session is shared *across processes*: `browser login` outlives itself.

    That is the whole point of the browser path (the clearance lives in the browser, not in a
    copied cookie), so the discovery file has to name something that survives the process that
    wrote it — the browser, not the CLI — and a later command has to be able to attach to it.
    """

    def setUp(self):
        if not any(shutil.which(binary) for binary in browser._CHROME_BINARIES):
            self.skipTest("no Chrome on this machine")
        try:
            import playwright.sync_api  # noqa: F401
        except ImportError:
            self.skipTest("playwright is not installed")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        cache = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or str(
            os.path.join(os.path.expanduser("~"), ".cache", "ms-playwright"))
        self.env = mock.patch.dict(os.environ, {"XDG_DATA_HOME": self.tmp.name,
                                                "PYTHONPATH": REPO,
                                                "PLAYWRIGHT_BROWSERS_PATH": cache})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.session = Session(profile=paths.browser_profile_dir(), headless=True)
        # `terminate`, not `close`: three tests in this class each start a browser in a helper
        # subprocess, so nothing here owns one and `close` alone would leave one up per test.
        self.addCleanup(self.session.terminate, force=True)
        self.port = 0
        handler = functools.partial(_Handler, directory=self.tmp.name)
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.server.RequestHandlerClass.log_message = lambda *a: None
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/"

    def test_a_session_outlives_the_process_that_started_it_and_is_attachable(self):
        started = subprocess.run([sys.executable, "-c", CHILD], cwd=REPO, env=dict(os.environ),
                                 capture_output=True, text=True, timeout=120)
        self.assertEqual(started.returncode, 0, started.stderr)
        record = json.loads(paths.browser_session_file().read_text())
        self.assertNotEqual(record["pid"], os.getpid(), "the pid must be the browser's")
        self.assertTrue(self.session.alive())
        self.assertEqual(self.session.page(self.url).status, 200)

    def test_downloads_survive_the_attach(self):
        subprocess.run([sys.executable, "-c", CHILD], cwd=REPO, env=dict(os.environ),
                       capture_output=True, text=True, timeout=120, check=True)
        with open(os.path.join(self.tmp.name, "a.pdf"), "wb") as fh:
            fh.write(b"%PDF-1.4\n%fake\n")
        self.assertTrue(self.session.download(self.url + "a.pdf").startswith(b"%PDF"))

    def test_a_pdf_rendered_inline_is_read_from_the_response(self):
        subprocess.run([sys.executable, "-c", CHILD], cwd=REPO, env=dict(os.environ),
                       capture_output=True, text=True, timeout=120, check=True)
        with open(os.path.join(self.tmp.name, "b.inline.pdf"), "wb") as fh:
            fh.write(b"%PDF-1.4\n%inline\n")
        self.assertTrue(self.session.download(self.url + "b.inline.pdf").startswith(b"%PDF"))


class AliveProbeTest(unittest.TestCase):
    """A discovery file whose broker is gone must read as dead."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"XDG_DATA_HOME": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        file = paths.browser_session_file()
        file.parent.mkdir(parents=True, exist_ok=True)
        self.file = file
        self.session = Session(headless=True)

    def test_a_control_port_nothing_answers_on_is_dead(self):
        self.file.write_text(json.dumps({"pid": os.getpid(), "control_port": 1,
                                         "cdp_port": 9333, "profile": "/x"}))
        self.assertFalse(self.session.alive())

    def test_no_discovery_file_is_dead(self):
        self.assertFalse(self.session.alive())


class MissingPlaywrightTest(unittest.TestCase):
    def test_the_error_names_the_install_command(self):
        # Reached in-process: the broker would be a child with its own imports, where the
        # ``sys.modules`` patch below does not reach.
        with mock.patch.dict(sys.modules, {"playwright.sync_api": None}), \
                mock.patch.dict(os.environ, {"XDG_DATA_HOME": tempfile.mkdtemp()}), \
                self.assertRaises(Exception) as ctx:
            browser.browser_broker._bundled_binary()
        self.assertIn("install", str(ctx.exception).lower())
