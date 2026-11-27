"""The ``[browser]`` section decides whether an entitled read goes through a session.

Off by default: nothing starts a browser for someone who configured cookies. When it is on,
the session must inherit the proxy httpx uses (the clearance is bound to the egress IP) and
must not insist on a Chrome that is not installed.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from litcli import browser, paths
from litcli.browser import session_from_config


class ResolveProfileTest(unittest.TestCase):
    def setUp(self):
        self._home = tempfile.TemporaryDirectory()
        self.addCleanup(self._home.cleanup)
        patcher = mock.patch.dict(os.environ, {"XDG_DATA_HOME": self._home.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        paths.browser_profile_dir().mkdir(parents=True, exist_ok=True)

    def test_empty_and_user_resolve_to_the_platform_default(self):
        self.assertEqual(paths.resolve_browser_profile(None), paths.user_browser_profile_dir())
        self.assertEqual(paths.resolve_browser_profile("user"), paths.user_browser_profile_dir())

    def test_dedicated_resolves_to_the_tools_own_dir(self):
        self.assertEqual(paths.resolve_browser_profile("dedicated"), paths.browser_profile_dir())

    def test_a_path_is_expanded(self):
        self.assertEqual(paths.resolve_browser_profile("~/elsewhere"),
                         Path("~/elsewhere").expanduser())

    def test_a_disabled_section_still_yields_none(self):
        self.assertIsNone(session_from_config({"browser": {"enabled": False}}))

    def test_the_source_is_reported(self):
        with mock.patch("shutil.which", return_value="/usr/bin/google-chrome"):
            default = session_from_config({"browser": {"enabled": True}})
            explicit = session_from_config({"browser": {"enabled": True, "profile": "dedicated"}})
        self.assertEqual(default._profile_source, "default")
        self.assertEqual(explicit._profile_source, "config")

    def test_the_migration_warning_fires_only_while_the_key_is_unset(self):
        cookie_db = paths.browser_profile_dir() / "Default" / "Cookies"
        cookie_db.parent.mkdir(parents=True, exist_ok=True)
        cookie_db.write_text("SQLite")
        with mock.patch("shutil.which", return_value="/usr/bin/google-chrome"), \
                mock.patch("litcli.debug.warn") as told:
            session_from_config({"browser": {"enabled": True}})
            session_from_config({"browser": {"enabled": True, "profile": "dedicated"}})
        told.assert_called_once()
        self.assertIn('profile = "dedicated"', told.call_args.args[0])


class SessionFromConfigTest(unittest.TestCase):
    def test_disabled_returns_none(self):
        self.assertIsNone(session_from_config({"browser": {"enabled": False}}))

    def test_absent_section_returns_none(self):
        self.assertIsNone(session_from_config({}))

    def test_enabled_builds_a_session(self):
        with mock.patch("shutil.which", return_value="/usr/bin/google-chrome"):
            session = session_from_config({"browser": {"enabled": True}})
        self.assertIsInstance(session, browser.Session)

    def test_the_proxy_is_pushed_to_the_browser(self):
        cfg = {"browser": {"enabled": True}, "network": {"proxy": "http://proxy:3128"}}
        with mock.patch("shutil.which", return_value="/usr/bin/google-chrome"):
            session = session_from_config(cfg)
        self.assertEqual(session._proxy, "http://proxy:3128")

    def test_the_profile_can_be_pointed_elsewhere(self):
        cfg = {"browser": {"enabled": True, "profile": "~/elsewhere"}}
        with mock.patch("shutil.which", return_value="/usr/bin/google-chrome"):
            session = session_from_config(cfg)
        self.assertEqual(str(session._profile), os.path.expanduser("~/elsewhere"))

    def test_the_idle_minutes_reach_the_broker(self):
        # The idle exit takes its delay from the config, the same way as the profile: an
        # install that wants a shorter-lived supervisor writes it once and every spawn obeys.
        cfg = {"browser": {"enabled": True, "idle_minutes": 0.5}}
        with mock.patch("shutil.which", return_value="/usr/bin/google-chrome"):
            session = session_from_config(cfg)
        seen = {}

        def factory(**kw):
            seen.update(kw)
            return mock.Mock()

        session._lease_factory = factory
        session._new_lease()
        self.assertEqual(seen["idle_minutes"], 0.5)

    def test_missing_chrome_falls_back_to_the_bundled_browser(self):
        with mock.patch("shutil.which", return_value=None):
            session = session_from_config({"browser": {"enabled": True}})
        self.assertIsNone(session._channel)


class SessionAttachedTest(unittest.TestCase):
    """``discover_backends`` hands the session to every entitled reader."""

    CFG: ClassVar[dict] = {"backends": {"wiley": {"type": "wiley", "cookie": "a=b", "user_agent": "UA/1"},
                        "arxiv": {"type": "arxiv"}}}

    def test_an_entitled_backend_gets_the_session(self):
        from litcli.config import discover_backends

        sentinel = object()
        with mock.patch("litcli.browser.session_from_config", return_value=sentinel):
            built = discover_backends(self.CFG)
        self.assertIs(built["wiley"].session, sentinel)

    def test_a_plain_backend_does_not(self):
        from litcli.config import discover_backends

        with mock.patch("litcli.browser.session_from_config", return_value=object()):
            built = discover_backends(self.CFG)
        self.assertIsNone(getattr(built["arxiv"], "session", None))

    def test_no_session_configured_keeps_the_cookie_path(self):
        from litcli.config import discover_backends

        with mock.patch("litcli.browser.session_from_config", return_value=None):
            built = discover_backends(self.CFG)
        self.assertIsNone(built["wiley"].session)
