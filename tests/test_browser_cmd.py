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

from litcli.commands import browser as cmd
from tests.fake_browser import FakeSession


def run(fn, **args):
    return fn({}, mock.Mock(**args))


class LoginTest(unittest.TestCase):
    def setUp(self):
        self.session = FakeSession()
        patcher = mock.patch.object(cmd, "Session", lambda **_: self.session)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_login_without_a_site_exits_2_and_lists_the_choices(self):
        args = mock.Mock(site=None)
        with mock.patch("builtins.print") as printed:
            self.assertEqual(cmd.cmd_browser_login({}, args), 2)
        out = " ".join(str(c.args[0]) for c in printed.call_args_list if c.args)
        self.assertIn("sciencedirect", out)

    def test_login_opens_the_site_and_leaves_the_browser_running(self):
        args = mock.Mock(site="ieee")
        with mock.patch("builtins.input", return_value=""):
            self.assertEqual(cmd.cmd_browser_login({}, args), 0)
        self.assertIn(cmd.SITES["ieee"], self.session.urls)
        self.assertFalse(self.session.closed)

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


if __name__ == "__main__":
    unittest.main()
