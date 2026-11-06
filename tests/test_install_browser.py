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
from litcli import paths
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
        argv, kwargs = call.call_args
        self.assertEqual(argv[0], ["/node", "/cli.js", "install", "webkit"])
        self.assertEqual(kwargs["env"]["PLAYWRIGHT_BROWSERS_PATH"],
                         str(paths.browser_cache_dir()))
        self.assertIn("webkit installed", out)

    def test_dry_run_reaches_the_driver(self):
        driver = mock.Mock()
        driver._impl._driver.compute_driver_executable.return_value = ("/node", "/cli.js")
        with mock.patch.dict(sys.modules, {"playwright": driver,
                                           "playwright._impl": driver._impl,
                                           "playwright._impl._driver": driver._impl._driver}), \
                mock.patch.object(diagnostics.subprocess, "call", return_value=0) as call:
            rc, _ = _run(["backends", "install-browser", "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertEqual(call.call_args[0][0],
                         ["/node", "/cli.js", "install", "--dry-run", "chromium"])

    def test_with_deps_reaches_the_driver(self):
        driver = mock.Mock()
        driver._impl._driver.compute_driver_executable.return_value = ("/node", "/cli.js")
        with mock.patch.dict(sys.modules, {"playwright": driver,
                                           "playwright._impl": driver._impl,
                                           "playwright._impl._driver": driver._impl._driver}), \
                mock.patch.object(diagnostics.subprocess, "call", return_value=0) as call:
            rc, _ = _run(["backends", "install-browser", "--with-deps"])
        self.assertEqual(rc, 0)
        self.assertEqual(call.call_args[0][0],
                         ["/node", "/cli.js", "install", "--with-deps", "chromium"])

    def test_check_launches_the_browser_headless(self):
        page = mock.MagicMock()
        page.chromium.launch.return_value.version = "123.0.1"
        module = mock.MagicMock()
        module.sync_playwright.return_value.__enter__.return_value = page
        with mock.patch.dict(sys.modules, {"playwright": module,
                                           "playwright.sync_api": module}):
            rc, out = _run(["backends", "install-browser", "--check"])
        self.assertEqual(rc, 0)
        page.chromium.launch.assert_called_once_with(headless=True)
        self.assertIn("123.0.1", out)

    def test_check_reports_a_browser_that_will_not_launch(self):
        page = mock.MagicMock()
        page.chromium.launch.side_effect = RuntimeError("Executable doesn't exist")
        module = mock.MagicMock()
        module.sync_playwright.return_value.__enter__.return_value = page
        with mock.patch.dict(sys.modules, {"playwright": module,
                                           "playwright.sync_api": module}):
            rc, out = _run(["backends", "install-browser", "--check"])
        self.assertEqual(rc, 2)
        self.assertIn("does not launch", out)

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
