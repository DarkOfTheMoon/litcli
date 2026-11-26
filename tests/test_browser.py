"""The browser session object: one running instance, discoverable, reusable.

The launcher is injected, so nothing here opens a browser.
"""
import functools
import http.server
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import litcli.browser as browser
import litcli.paths as paths
from litcli.browser import Session
from tests.fake_browser import FakeDownload, FakeLauncher

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
#: A child process that starts the shared session and then exits, like `browser login` does.
CHILD = """
import sys
from litcli import paths
from litcli.browser import Session
Session(profile=paths.browser_profile_dir(), headless=True).start()
print("up", flush=True)
"""


class SessionTest(unittest.TestCase):
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
        self.launcher = FakeLauncher()
        self.s = Session(launcher=self.launcher)

    def test_a_second_start_reuses_the_running_one(self):
        self.s.start()
        self.s.start()
        self.assertEqual(self.launcher.calls, 1)
        self.assertTrue(self.s.alive())

    def test_start_writes_and_close_removes_the_session_file(self):
        self.s.start()
        self.assertTrue(paths.browser_session_file().exists())
        self.s.close()
        self.assertFalse(paths.browser_session_file().exists())

    def test_terminate_stops_the_browser_this_process_started(self):
        # `close` leaves a detached browser up so the clearance survives a later command; a
        # survey run is one of many and would otherwise leave one browser behind per run.
        self.s.start()
        # the second answer is the liveness poll: the browser is gone
        with mock.patch("litcli.browser.os.kill",
                        side_effect=[None, ProcessLookupError]) as killed:
            self.s.terminate()
        self.assertEqual(killed.call_args_list[0], mock.call(os.getpid(), signal.SIGTERM))
        self.assertFalse(paths.browser_session_file().exists())

    def test_terminate_leaves_a_browser_someone_else_started(self):
        self.s.start()
        self.s.close()  # disconnect; the "other process" browser lives on
        with mock.patch("litcli.browser.os.kill") as killed:
            self.s.terminate()
        killed.assert_not_called()

    def test_terminate_force_stops_a_browser_started_elsewhere(self):
        # A browser someone else started (the process that ran `browser login`, or a test's
        # helper subprocess) is not `terminate`'s by ownership, but whoever holds the project
        # can still want it gone: `forcebal` names it the only way it is known — the file.
        paths.browser_session_file().write_text(json.dumps({"pid": 4242, "port": 1}))
        with mock.patch("litcli.browser.os.kill",
                        side_effect=[None, ProcessLookupError]) as killed:
            self.s.terminate(force=True)
        self.assertEqual(killed.call_args_list[0], mock.call(4242, signal.SIGTERM))
        self.assertFalse(paths.browser_session_file().exists())

    def test_reset_parks_the_page_on_about_blank(self):
        # A saved file left up in the tab keeps a --pdf-renderer spinning after the driver has
        # exited, and its signed address dies in minutes: the desktop shows a dead link and
        # reads as a command that hung.
        self.s.start()
        page = self.s._runtime.context.pages[0]
        page.url = "https://pdf.sciencedirectassets.com/x.am.pdf?sig=1"
        self.s.reset()
        self.assertEqual(page.url, "about:blank")
        self.assertTrue(self.s.alive())  # parked, not closed: the clearance survives

    def test_reset_without_a_browser_is_quiet(self):
        self.s.reset()  # no session file, nothing to park

    def test_proxy_reaches_the_launcher(self):
        Session(proxy="http://127.0.0.1:7890", launcher=self.launcher).start()
        self.assertEqual(self.launcher.kwargs["proxy"], "http://127.0.0.1:7890")

    def test_a_challenge_that_solves_is_waited_out(self):
        # A managed challenge paints "Just a moment..." first and replaces itself ~10 s later;
        # judged on the first paint every gated site reads as unusable while it is a direct
        # load away from the article.
        self.s.start()
        self.launcher.queue.append((403, "<html><title>Just a moment...</title></html>",
                                    "<html><title>APS Journals</title></html>"))
        answer = self.s.page("https://link.aps.org/")
        self.assertEqual((answer.status, "Just a moment" in answer.text), (200, False))

    def test_a_challenge_that_never_solves_keeps_its_gate(self):
        self.s.start()
        self.launcher.queue.append((403, "<html><title>Just a moment...</title></html>"))
        with mock.patch.object(browser, "_CHALLENGE_WAIT", 0):
            answer = self.s.page("https://link.aps.org/")
        self.assertEqual(answer.status, 403)

    def test_a_challenge_that_outlasts_the_wait_is_navigated_once_more(self):
        # The clearance cookie belongs to the exchange, so a host that paces it past the wait
        # clears on the next load rather than this one: one more navigation is a second chance
        # at the article, not a second guess.
        self.s.start()
        self.launcher.queue.extend([(403, "<html><title>Just a moment...</title></html>"),
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
        self.launcher.queue.append((403, "<html>__cf_chl</html>"))
        with mock.patch.object(browser, "_CHALLENGE_WAIT", 0):
            self.assertEqual(self.s.page("https://x/a").status, 403)

    def test_a_download_returns_the_bytes(self):
        self.s.start()
        self.launcher.queue.append(b"%PDF-1.4 body")
        self.assertTrue(self.s.download("https://x/a.pdf").startswith(b"%PDF"))

    def test_a_pdf_gets_five_minutes_to_arrive(self):
        # The wait covers the challenge being solved and a slow link alike; four seconds cut
        # both off and sent the request context at the interstitial instead of the file.
        self.assertEqual(browser._DOWNLOAD_WAIT, 300.0)

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
        self.assertEqual(kw["timeout"], browser._DOWNLOAD_WAIT * 1000)
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
                self.assertRaises(browser.Unavailable):
            self.s.download("https://x/a.pdf")

    def test_running_browsers_are_found_without_their_proc_entry_matching(self):
        # Chrome rewrites its own argv into one space-joined string, so NUL-splitting
        # /proc/<pid>/cmdline never yields an argument and a per-argument match finds nothing.
        # A profile that only shares a prefix with ours is someone else's browser.
        proc = Path(self.tmp.name) / "proc"
        mine = str(paths.browser_profile_dir())

        def entry(pid, text):
            (proc / pid).mkdir(parents=True)
            (proc / pid / "cmdline").write_bytes(text.encode())

        entry("100", f"/opt/google/chrome/chrome --headless=new --user-data-dir={mine} "
                     "--remote-debugging-port=9333 --no-first-run about:blank")
        entry("101", "\0".join(["/opt/google/chrome/chrome", f"--user-data-dir={mine}",
                                "--remote-debugging-port=9444"]))
        entry("102", f"/opt/google/chrome/chrome --user-data-dir={mine}2 "
                     "--remote-debugging-port=9555")
        entry("103", f"/opt/google/chrome/chrome --type=zygote --user-data-dir={mine} "
                     "--remote-debugging-port=9666")
        entry("104", f"/opt/google/chrome/chrome --user-data-dir={mine} --no-first-run")
        (proc / "self").mkdir()
        # 103 is a child (its port is the parent's): reaping it would kill a renderer, so it is
        # left out. 104 is a browser started without a debugging port — invisible, but a browser.
        self.assertEqual(browser._running_on(paths.browser_profile_dir(), proc),
                         [(100, 9333), (101, 9444), (104, None)])

    def test_a_portless_leftover_is_stopped_before_the_launch(self):
        # Two browsers cannot share a profile: the second answers "Opening in existing browser
        # session" and exits. A port-less browser cannot be attached, so it has to go before
        # the launch — reaping after it is too late, the launch has already lost.
        order = []
        self.s._launcher = lambda *a, **k: (order.append("launch"), self.launcher(*a, **k))[1]
        with mock.patch.object(browser, "_running_on", lambda profile: [(4242, None)]), \
                mock.patch("litcli.browser.os.kill", lambda pid, sig: order.append("kill")):
            self.s.start()
        self.assertEqual(order[:2], ["kill", "launch"])

    def test_a_headless_browser_is_replaced_when_a_window_is_wanted(self):
        # A sign-in needs a window, and a headless browser has none: handing one back to
        # `browser login` looks like the command did nothing.
        with mock.patch.object(browser, "_running_on", lambda profile: [(4242, 9333)]), \
                mock.patch.object(browser, "_is_headless", lambda pid: True), \
                mock.patch("litcli.browser.os.kill", lambda pid, sig: None), \
                mock.patch.object(browser, "_wait_gone", lambda pid, timeout=3.0: None):
            self.s.start()
        self.assertEqual(self.launcher.calls, 1)

    def test_a_headless_browser_is_kept_when_headless_is_wanted(self):
        self.s = Session(headless=True, launcher=self.launcher)
        with mock.patch.object(browser, "_running_on", lambda profile: [(4242, 9333)]), \
                mock.patch.object(browser, "_is_headless", lambda pid: True), \
                mock.patch("litcli.browser.os.kill"):
            self.s.start()
        self.assertEqual(self.launcher.calls, 0)

    def test_a_visible_browser_serves_a_headless_run_too(self):
        with mock.patch.object(browser, "_running_on", lambda profile: [(4242, 9333)]), \
                mock.patch.object(browser, "_is_headless", lambda pid: False), \
                mock.patch("litcli.browser.os.kill"):
            self.s = Session(headless=True, launcher=self.launcher)
            self.s.start()
        self.assertEqual(self.launcher.calls, 0)

    def test_a_launch_that_loses_the_race_adopts_the_winner(self):
        # A browser can appear between the scan and the launch; the launch then exits at once
        # with "Opening in existing browser session". The session exists either way.
        running = []

        def losing_launch(*_a, **_k):
            running.append((5151, 9292))  # the winner, one instant too late for the scan
            raise browser.BrowserUnavailable("chrome exited at once (code 0)")

        self.s._launcher = losing_launch
        with mock.patch.object(browser, "_running_on", lambda profile: list(running)), \
                mock.patch("litcli.browser.os.kill"):
            self.s.start()
        info = json.loads(paths.browser_session_file().read_text())
        self.assertEqual((info["pid"], info["port"]), (5151, 9292))

    def test_a_failed_launch_with_nobody_else_up_still_raises(self):
        self.s._launcher = mock.Mock(side_effect=browser.BrowserUnavailable("no display"))
        with mock.patch.object(browser, "_running_on", lambda profile: []), \
                self.assertRaises(browser.BrowserUnavailable):
            self.s.start()

    def test_a_browser_whose_file_was_lost_is_adopted_not_relaunched(self):
        # A crashed run or a cleaned /tmp takes the discovery file and leaves the browser: it is
        # still there, still signed in, and starting a second one per command is what fills the
        # machine (each is ~10 processes).
        with mock.patch.object(browser, "_running_on",
                               lambda profile: [(4242, 9333), (4343, 9444)]), \
                mock.patch("litcli.browser.os.kill") as killed:
            self.s.start()
        self.assertEqual(self.launcher.calls, 0)
        info = json.loads(paths.browser_session_file().read_text())
        self.assertEqual((info["pid"], info["port"]), (4242, 9333))
        killed.assert_called_once_with(4343, signal.SIGTERM)  # the duplicate goes

    def test_a_leftover_the_launch_replaces_is_stopped(self):
        # Reuse only works for a browser that can be talked to; one with no debugging port is
        # invisible to CDP (and holds the profile lock), so a launch follows and the invisible
        # one must not be left running.
        with mock.patch.object(browser, "_running_on", lambda profile: [(4242, None)]), \
                mock.patch("litcli.browser.os.kill") as killed:
            self.s.start()
        self.assertEqual(self.launcher.calls, 1)
        killed.assert_any_call(4242, signal.SIGTERM)

    def test_a_stale_pid_is_not_alive_and_is_replaced(self):
        paths.browser_session_file().write_text('{"pid": 999999, "profile": "/x"}')
        self.assertFalse(self.s.alive())
        self.s.start()
        self.assertEqual(self.launcher.calls, 1)

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
    """A discovery file whose browser is gone must read as dead even if its pid is reused."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"XDG_DATA_HOME": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        file = paths.browser_session_file()
        file.parent.mkdir(parents=True, exist_ok=True)
        self.file = file
        self.session = Session(headless=True, launcher=FakeLauncher())

    def test_a_live_pid_behind_a_dead_debugging_port_is_not_alive(self):
        self.file.write_text(json.dumps({"pid": os.getpid(), "port": 1, "profile": "/x"}))
        self.assertFalse(self.session.alive())

    def test_a_live_pid_with_no_port_keeps_the_old_meaning(self):
        self.file.write_text(json.dumps({"pid": os.getpid(), "profile": "/x"}))
        self.assertTrue(self.session.alive())


class MissingPlaywrightTest(unittest.TestCase):
    def test_the_error_names_the_install_command(self):
        with mock.patch.object(browser, "_CHROME_BINARIES", ()), \
                mock.patch.dict(sys.modules, {"playwright.sync_api": None}), \
                mock.patch.object(paths, "_IS_WINDOWS", False), \
                mock.patch.dict(os.environ, {"XDG_DATA_HOME": tempfile.mkdtemp()}), \
                self.assertRaises(Exception) as ctx:
            Session().start()
        self.assertIn("install", str(ctx.exception).lower())
