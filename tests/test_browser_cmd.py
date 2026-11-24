"""`litcli browser login` / `litcli browser status`.

One shared profile means one sign-in covers every publisher; these two commands are the
human ends of that — open the window at the site to sign in, and say which sites the live
session currently reaches.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import litcli.browser as browser_mod
from litcli.commands import browser as cmd
from tests.fake_browser import FakeSession


def run(fn, **args):
    return fn({}, mock.Mock(**args))


class LoginTest(unittest.TestCase):
    def setUp(self):
        self.session = FakeSession()
        self.kwargs = {}

        def build(**kw):  # every test goes through the real session_from_config
            self.kwargs.update(kw)
            return self.session

        patcher = mock.patch.object(browser_mod, "Session", build)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_login_without_a_site_exits_2_and_lists_the_choices(self):
        args = mock.Mock(site=None)
        with mock.patch("builtins.print") as printed:
            self.assertEqual(cmd.cmd_browser_login({}, args), 2)
        out = " ".join(str(c.args[0]) for c in printed.call_args_list if c.args)
        self.assertIn("sciencedirect", out)

    def test_the_window_it_opens_is_the_configured_one(self):
        # `[browser].profile`/`[network].proxy` are what `fetch` will use; a login window built
        # from different settings would sign in to a browser nothing else looks at. It must
        # also work while `[browser].enabled` is still false — login is how you set it up.
        cfg = {"browser": {"profile": "/tmp/litcli-profile"},
               "network": {"proxy": "http://127.0.0.1:7890"}}
        with mock.patch("builtins.input", return_value=""):
            self.assertEqual(cmd.cmd_browser_login(cfg, mock.Mock(site="wiley")), 0)
        self.assertEqual(self.kwargs["profile"], "/tmp/litcli-profile")
        self.assertEqual(self.kwargs["proxy"], "http://127.0.0.1:7890")

    def test_login_opens_the_site_and_leaves_the_browser_running(self):
        # Disconnect, but do not stop: the driver would otherwise be torn down by interpreter
        # exit with its CDP socket still open, and node throws an unhandled EPIPE over the
        # prompt the user has just got back. `close` is the disconnect; the browser stays up.
        args = mock.Mock(site="ieee")
        with mock.patch("builtins.input", return_value=""), \
                mock.patch("builtins.print"):
            self.assertEqual(cmd.cmd_browser_login({}, args), 0)
        self.assertIn(cmd.SITES["ieee"], self.session.urls)
        self.assertTrue(self.session.closed)
        self.assertFalse(self.session.terminated)

    def test_the_wait_for_enter_is_placed_in_the_terminal(self):
        # The command only ends on an Enter, and while its window has the focus the obvious
        # Enter goes to the page instead: the wait stays, but the prompt says where.
        with mock.patch("builtins.input", return_value=""), \
                mock.patch("builtins.print") as printed:
            cmd.cmd_browser_login({}, mock.Mock(site="ieee"))
        out = " ".join(str(c.args[0]) for c in printed.call_args_list if c.args)
        self.assertIn("Enter here", out)

    def test_status_classifies_each_site(self):
        self.session.queue = [(200, "<html>Sign in to IEEE</html>"),
                              (403, "<html>cf-mitigated</html>"),
                              (200, '<meta name="citation_title" content="A paper">')]
        args = mock.Mock(site="aip")
        with mock.patch("builtins.print") as printed:
            self.assertEqual(cmd.cmd_browser_status({}, args), 0)
        out = " ".join(str(a) for c in printed.call_args_list for a in c.args)
        self.assertIn("signed out", out)
        self.assertIn("gated", out)
        self.assertIn("signed in", out)

    def test_a_site_that_does_not_answer_stays_one_line(self):
        # A dead site answers with a Playwright error carrying a multi-line call log; dropped
        # into the table raw it splits the row. A timeout is also not a site refusal: it gets
        # its own word instead of reading like the site said no.
        from litcli.browser import PlaywrightTimeoutError
        timeouts = len(cmd.SITES)
        # `status` gives a timeout one retry, so a site that never answers costs two.
        self.session.queue = ([Exception("net::ERR_NAME_NOT_RESOLVED\nCall log:\n - x")]
                              + [PlaywrightTimeoutError("Page.goto: Timeout 30000ms exceeded.\n"
                                                        "Call log:\n  - navigating")]
                              * (2 * (timeouts - 1)))
        args = mock.Mock(site="aip")
        with mock.patch("builtins.print") as printed:
            self.assertEqual(cmd.cmd_browser_status({}, args), 0)
        lines = [str(c.args[0]) for c in printed.call_args_list if c.args]
        rows = [line for line in lines if "  " in line]
        self.assertEqual(len(rows), timeouts)
        self.assertEqual(sum("no answer" in line for line in rows), timeouts - 1)
        self.assertTrue(any("error:" in line and "Call log" not in line for line in rows))

    def test_a_site_that_times_out_once_is_probed_again(self):
        # A slow site is not a refusal; one second chance separates the two.
        from litcli.browser import PlaywrightTimeoutError
        self.session.queue = [(PlaywrightTimeoutError("Page.goto: Timeout 30000ms exceeded.")),
                              (200, '<meta name="citation_title" content="A paper">')]
        with mock.patch("builtins.print") as printed:
            self.assertEqual(cmd.cmd_browser_status({}, mock.Mock(site="aip")), 0)
        first = next(str(c.args[0]) for c in printed.call_args_list
                     if c.args and "  " in str(c.args[0]))
        self.assertIn("signed in", first)

    def test_status_disconnects_whatever_it_attached(self):
        # The browser is already running here (a login from an earlier command), so `status` only
        # attaches. Left attached, its Playwright driver stays active in the process and the next
        # session start fails with "Sync API inside the asyncio loop".
        self.session.queue = [(200, "<html>signed out</html>")] * len(cmd.SITES)
        with mock.patch("builtins.print"):
            self.assertEqual(cmd.cmd_browser_status({}, mock.Mock(site="aip")), 0)
        self.assertTrue(self.session.closed)
        self.assertFalse(self.session.terminated)  # the user opened this one

    def test_status_stops_the_browser_it_started_itself(self):
        # A probe must not leave a browser behind: nothing was up, this command started one,
        # and it is the one that stops it.
        self.session.alive = lambda: False
        self.session.queue = [(200, "<html>signed out</html>")] * len(cmd.SITES)
        with mock.patch("builtins.print"):
            self.assertEqual(cmd.cmd_browser_status({}, mock.Mock(site="aip")), 0)
        self.assertTrue(self.session.terminated)


if __name__ == "__main__":
    unittest.main()
