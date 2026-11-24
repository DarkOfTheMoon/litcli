"""The ``[browser]`` section decides whether an entitled read goes through a session.

Off by default: nothing starts a browser for someone who configured cookies. When it is on,
the session must inherit the proxy httpx uses (the clearance is bound to the egress IP) and
must not insist on a Chrome that is not installed.
"""
import os
import sys
import unittest
from typing import ClassVar
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from litcli import browser
from litcli.browser import session_from_config


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
