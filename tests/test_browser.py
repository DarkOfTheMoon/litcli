"""The browser session object: one running instance, discoverable, reusable.

The launcher is injected, so nothing here opens a browser.
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import litcli.paths as paths
from litcli.browser import Session
from tests.fake_browser import FakeLauncher


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

    def test_proxy_reaches_the_launcher(self):
        Session(proxy="http://127.0.0.1:7890", launcher=self.launcher).start()
        self.assertEqual(self.launcher.kwargs["proxy"], "http://127.0.0.1:7890")

    def test_a_gate_page_is_returned_not_raised(self):
        self.s.start()
        self.launcher.queue.append((403, "<html>__cf_chl</html>"))
        self.assertEqual(self.s.page("https://x/a").status, 403)

    def test_a_download_returns_the_bytes(self):
        self.s.start()
        self.launcher.queue.append(b"%PDF-1.4 body")
        self.assertTrue(self.s.download("https://x/a.pdf").startswith(b"%PDF"))

    def test_a_stale_pid_is_not_alive_and_is_replaced(self):
        paths.browser_session_file().write_text('{"pid": 999999, "profile": "/x"}')
        self.assertFalse(self.s.alive())
        self.s.start()
        self.assertEqual(self.launcher.calls, 1)

    def test_alive_is_false_once_closed(self):
        self.s.start()
        self.s.close()
        self.assertFalse(self.s.alive())


class MissingPlaywrightTest(unittest.TestCase):
    def test_the_error_names_the_install_command(self):
        def boom(*_a, **_k):
            raise ImportError("no playwright")

        s = Session()
        with mock.patch("litcli.browser._default_launcher", boom), \
                mock.patch.object(paths, "_IS_WINDOWS", False), \
                mock.patch.dict(os.environ, {"XDG_DATA_HOME": tempfile.mkdtemp()}), self.assertRaises(Exception) as ctx:
            s.start()
        self.assertIn("install", str(ctx.exception).lower())
