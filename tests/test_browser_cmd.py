"""`litcli browser login` / `litcli browser status`.

One shared profile means one sign-in covers every publisher; these two commands are the
human ends of that — open the window at the site to sign in, and say which sites the live
session currently reaches.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import litcli.browser as browser_mod
from litcli import paths
from litcli.browser import BrowserUnavailable
from litcli.commands import browser as cmd
from tests.fake_browser import FakeLease, FakeSession


def run(fn, **args):
    return fn({}, mock.Mock(**args))


def _state(file_cls=unittest.TestCase, **kw):
    """A discovery file as a live broker would leave it."""
    info = {"pid": 7, "control_port": 1, "cdp_port": 2, "owns": True, "headless": False,
            "leases": 0, "profile": "/p", "profile_directory": "Default", "source": "default"}
    info.update(kw)
    paths.browser_session_file().parent.mkdir(parents=True, exist_ok=True)
    paths.browser_session_file().write_text(json.dumps(info))
    return info


class _WithDataHome(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        env = mock.patch.dict(os.environ, {"XDG_DATA_HOME": self.home.name})
        env.start()
        self.addCleanup(env.stop)


class StopTest(_WithDataHome):
    def test_no_session_is_exit_0(self):
        with mock.patch("builtins.print") as printed:
            self.assertEqual(cmd.cmd_browser_stop({}, mock.Mock()), 0)
        self.assertIn("no session", " ".join(str(c.args[0]) for c in printed.call_args_list
                                             if c.args))

    def test_an_attached_browser_is_left_running(self):
        _state(owns=False, headless=True)
        with mock.patch.object(cmd.broker, "connect", return_value=FakeLease(owns=False)), \
                mock.patch.object(cmd.broker, "shutdown"), \
                mock.patch("builtins.print") as printed:
            self.assertEqual(cmd.cmd_browser_stop({}, mock.Mock()), 0)
        self.assertIn("your Chrome was left running",
                      " ".join(str(c.args[0]) for c in printed.call_args_list if c.args))

    def test_a_headless_browser_we_started_is_stopped(self):
        _state(owns=True, headless=True)
        with mock.patch.object(cmd.broker, "connect", return_value=FakeLease(owns=True)), \
                mock.patch.object(cmd.broker, "shutdown") as down, \
                mock.patch("builtins.print") as printed:
            self.assertEqual(cmd.cmd_browser_stop({}, mock.Mock()), 0)
        down.assert_called_once()
        self.assertIn("stopped (pid 7)", " ".join(str(c.args[0]) for c in printed.call_args_list
                                                  if c.args))

    def test_a_headed_browser_we_started_is_left_for_the_window(self):
        _state(owns=True, headless=False)
        with mock.patch.object(cmd.broker, "connect", return_value=FakeLease(owns=True)), \
                mock.patch.object(cmd.broker, "shutdown"), \
                mock.patch("builtins.print") as printed:
            self.assertEqual(cmd.cmd_browser_stop({}, mock.Mock()), 0)
        self.assertIn("close the window to end it",
                      " ".join(str(c.args[0]) for c in printed.call_args_list if c.args))

    def test_a_stale_file_is_no_session(self):
        _state(control_port=1)
        with mock.patch.object(cmd.broker, "connect",
                               side_effect=cmd.broker.BrokerUnavailable("gone")), \
                mock.patch("builtins.print") as printed:
            self.assertEqual(cmd.cmd_browser_stop({}, mock.Mock()), 0)
        self.assertIn("no session", " ".join(str(c.args[0]) for c in printed.call_args_list
                                             if c.args))


class LoginTest(_WithDataHome):
    def setUp(self):
        super().setUp()
        self.session = FakeSession()
        self.kwargs = {}

        def build(**kw):  # every test goes through the real session_from_config
            self.kwargs.update(kw)
            return self.session

        patcher = mock.patch.object(browser_mod, "Session", build)
        patcher.start()
        self.addCleanup(patcher.stop)
        # The window path is the own-profile one; the session path is what these cover.
        own = mock.patch.object(cmd, "_targets_the_users_own_profile", return_value=False)
        own.start()
        self.addCleanup(own.stop)
        for name in ("pin", "shutdown", "status"):
            p = mock.patch.object(cmd.broker, name, return_value={})
            p.start()
            self.addCleanup(p.stop)

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

    def test_an_attached_window_is_only_warmed(self):
        self.session.mode = "attached"
        with mock.patch.object(cmd.broker, "pin") as pinned, \
                mock.patch("builtins.input", return_value=""), mock.patch("builtins.print"):
            self.assertEqual(cmd.cmd_browser_login({}, mock.Mock(site="ieee")), 0)
        self.assertEqual(self.session.urls, [cmd.SITES["ieee"]])
        self.assertFalse(self.session.terminated)
        pinned.assert_not_called()  # the broker already runs; only a launch pins

    def test_a_headless_browser_in_use_makes_login_wait(self):
        self.session.mode = "launched"
        self.session._lease.headless = True
        with mock.patch.object(cmd.broker, "status", return_value={"leases": 1}), \
                mock.patch.object(cmd, "_WAIT_FOR_IDLE", 0.05), \
                mock.patch("builtins.print") as printed:
            self.assertEqual(cmd.cmd_browser_login({}, mock.Mock(site="ieee")), 2)
        out = " ".join(str(c.args[0]) for c in printed.call_args_list if c.args)
        self.assertIn("someone is using the browser", out)
        self.assertEqual(self.session.urls, [])

    def test_a_headless_browser_with_nobody_on_it_is_replaced_by_a_window(self):
        # A hidden browser's window never shows, so handing it back to `login` reads as the
        # command doing nothing. Nobody holds a lease, so it is stopped and a window opened.
        first, second = self.session, FakeSession()
        built = []

        def build(**kw):
            built.append(kw)
            return first if len(built) == 1 else second

        self.session.mode = "launched"
        self.session._lease.headless = True
        with mock.patch.object(browser_mod, "Session", build), \
                mock.patch.object(cmd.broker, "status", return_value={"leases": 0}), \
                mock.patch.object(cmd, "_WAIT_FOR_IDLE", 0.05), \
                mock.patch("builtins.input", return_value=""), mock.patch("builtins.print"):
            self.assertEqual(cmd.cmd_browser_login({}, mock.Mock(site="ieee")), 0)
        self.assertTrue(first.terminated)
        self.assertTrue(second.started)
        self.assertEqual(second.urls, [cmd.SITES["ieee"]])
        self.assertIs(built[1]["headless"], False)
        self.assertTrue(built[1]["pinned"])

    def test_the_users_own_profile_is_opened_as_a_plain_window(self):
        # Chrome opens no debugging port at all when --user-data-dir is its default data
        # directory, so no broker could reach a browser on the user's own profile. The sign-ins
        # still land in that profile, which is all the headless reader needs.
        # A window needs a display; the runners have none, so name one here rather than
        # depending on wherever this runs (the guard below would answer before `_open_window`).
        with mock.patch.object(cmd, "_targets_the_users_own_profile", return_value=True), \
                mock.patch.object(cmd, "_open_window") as opened, \
                mock.patch.dict(os.environ, {"DISPLAY": ":0"}), \
                mock.patch("builtins.input", return_value=""), mock.patch("builtins.print"):
            self.assertEqual(cmd.cmd_browser_login({}, mock.Mock(site="ieee")), 0)
        self.assertEqual(opened.call_args.args[-1], cmd.SITES["ieee"])
        self.assertFalse(self.session.started)

    def test_the_window_path_still_says_what_to_do(self):
        with mock.patch.object(cmd, "_targets_the_users_own_profile", return_value=True), \
                mock.patch.object(cmd, "_open_window"), \
                mock.patch.dict(os.environ, {"DISPLAY": ":0"}), \
                mock.patch("builtins.input", return_value=""), \
                mock.patch("builtins.print") as printed:
            cmd.cmd_browser_login({}, mock.Mock(site="ieee"))
        out = " ".join(str(c.args[0]) for c in printed.call_args_list if c.args)
        self.assertIn("Sign in to ieee", out)
        self.assertIn("Enter here", out)

    def test_a_window_that_will_not_start_is_exit_2(self):
        # The display must exist, or the guard answers first and the window failure is never
        # reached (its own sentence also says "no display", so this case used to pass anyway).
        with mock.patch.object(cmd, "_targets_the_users_own_profile", return_value=True), \
                mock.patch.object(cmd, "_open_window", side_effect=BrowserUnavailable("no display")), \
                mock.patch.dict(os.environ, {"DISPLAY": ":0"}), \
                mock.patch("builtins.print") as printed:
            self.assertEqual(cmd.cmd_browser_login({}, mock.Mock(site="ieee")), 2)
        self.assertIn("no display", " ".join(str(c.args[0]) for c in printed.call_args_list))

    def test_status_prints_the_profile_header(self):
        self.session.mode = "attached"
        self.session.queue = [(200, "<html>signed in</html>")] * len(cmd.SITES)
        info = {"profile": "/p", "source": "default", "profile_directory": "Profile 1",
                "mode": "attached", "leases": 2}
        with mock.patch.object(cmd.broker, "status", return_value=info), \
                mock.patch("builtins.print") as printed:
            self.assertEqual(cmd.cmd_browser_status({}, mock.Mock(site="aip")), 0)
        out = " ".join(str(a) for c in printed.call_args_list for a in c.args)
        for part in ("/p", "default", "Profile 1", "attached", "2"):
            self.assertIn(part, out)

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

    def test_status_leaves_the_broker_it_started_running(self):
        # The broker outlives the command now (it idles out on its own), so the probe only
        # disconnects -- killing it here would drop a browser another command could reuse.
        self.session.alive = lambda: False
        self.session.queue = [(200, "<html>signed out</html>")] * len(cmd.SITES)
        with mock.patch("builtins.print"):
            self.assertEqual(cmd.cmd_browser_status({}, mock.Mock(site="aip")), 0)
        self.assertTrue(self.session.closed)
        self.assertFalse(self.session.terminated)


if __name__ == "__main__":
    unittest.main()


class OwnProfileTest(unittest.TestCase):
    """Which profile counts as the user's own: the one that cannot carry a debugging port."""

    def test_the_empty_setting_and_the_user_spelling_mean_it(self):
        self.assertTrue(cmd._targets_the_users_own_profile({}))
        self.assertTrue(cmd._targets_the_users_own_profile({"browser": {"profile": ""}}))
        self.assertTrue(cmd._targets_the_users_own_profile({"browser": {"profile": "user"}}))

    def test_another_directory_does_not(self):
        self.assertFalse(cmd._targets_the_users_own_profile({"browser": {"profile": "dedicated"}}))
        self.assertFalse(cmd._targets_the_users_own_profile({"browser": {"profile": "/tmp/x"}}))


class WindowTest(unittest.TestCase):
    def test_the_window_carries_the_configured_directory_and_proxy(self):
        cfg = {"browser": {"channel": "google-chrome", "profile_directory": "Profile 1"},
               "network": {"proxy": "http://127.0.0.1:7890"}}
        with mock.patch.object(cmd.broker, "_binary",
                               return_value="/opt/google/chrome/chrome"), \
                mock.patch.object(cmd.shutil, "which", return_value="/opt/g"), \
                mock.patch.object(cmd.subprocess, "Popen") as popen:
            cmd._open_window(cfg, "https://x/")
        argv = popen.call_args.args[0]
        self.assertIn("--profile-directory=Profile 1", argv)
        self.assertIn("--proxy-server=http://127.0.0.1:7890", argv)
        self.assertEqual(argv[-1], "https://x/")

    def test_the_profile_directory_is_passed_to_the_bundled_browser(self):
        # The bundled Chromium has no default directory of its own here, so the profile has to
        # be named or the sign-ins would land in a different browser's store.
        with mock.patch.object(cmd.broker, "_binary", return_value="/b/chromium"), \
                mock.patch.object(cmd.shutil, "which", return_value=None), \
                mock.patch.object(cmd.paths, "user_browser_profile_dir",
                                  return_value=Path("/u")) as own, \
                mock.patch.object(cmd.subprocess, "Popen") as popen:
            cmd._open_window({}, "https://x/")
        self.assertIn("--user-data-dir=/u", popen.call_args.args[0])
        own.assert_called()
