"""``backends install-browser``: the binary builds' browser download step.

Offline: Playwright's driver is patched, so only the argument plumbing and the error paths run.
"""
import contextlib
import io
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import litcli.commands.diagnostics as diagnostics
from litcli.commands.parser import build_parser


def _run(argv):
    args = build_parser().parse_args(argv)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = diagnostics.cmd_backends_install_browser({}, args)
    return rc, buf.getvalue()


class InstallBrowserTest(unittest.TestCase):
    def test_missing_playwright_is_an_error_with_a_hint(self):
        with mock.patch.dict(sys.modules, {"playwright": None,
                                           "playwright._impl": None,
                                           "playwright._impl._driver": None}):
            rc, out = _run(["backends", "install-browser"])
        self.assertEqual(rc, 2)
        self.assertIn("no playwright", out)

    def test_driver_receives_the_install_command(self):
        driver = mock.Mock()
        driver._impl._driver.compute_driver_executable.return_value = ("/node", "/cli.js")
        with mock.patch.dict(sys.modules, {"playwright": driver,
                                           "playwright._impl": driver._impl,
                                           "playwright._impl._driver": driver._impl._driver}), \
                mock.patch.object(diagnostics.subprocess, "call", return_value=0) as call:
            rc, out = _run(["backends", "install-browser", "webkit"])
        self.assertEqual(rc, 0)
        call.assert_called_once_with(["/node", "/cli.js", "install", "webkit"])
        self.assertIn("webkit installed", out)

    def test_failed_download_is_reported(self):
        driver = mock.Mock()
        driver._impl._driver.compute_driver_executable.return_value = ("/node", "/cli.js")
        with mock.patch.dict(sys.modules, {"playwright": driver,
                                           "playwright._impl": driver._impl,
                                           "playwright._impl._driver": driver._impl._driver}), \
                mock.patch.object(diagnostics.subprocess, "call", return_value=3):
            rc, out = _run(["backends", "install-browser"])
        self.assertEqual(rc, 2)
        self.assertIn("exit 3", out)


if __name__ == "__main__":
    unittest.main()
