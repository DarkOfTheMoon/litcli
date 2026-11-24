"""Entitled readers take their page and their PDF from the live browser session.

The cookie path stays exactly as it was: a session that is absent or already dead must fall
back to it, so the eleven publisher suites and every configured cookie keep working.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from litcli.backends import wiley
from tests.fake_browser import FakeSession
from tests.http_stub import stub

DOI = "10.1002/advs.202301234"
PARTICLE = b"%PDF-1.7\n%body"
PAGE = ('<html><head><title>A paper</title>'
        '<meta name="citation_title" content="A paper">'
        f'<meta name="citation_pdf_url" content="https://onlinelibrary.wiley.com/doi/pdfdirect/{DOI}">'
        '</head><body></body></html>')


class SessionServedTest(unittest.TestCase):
    def setUp(self):
        self.session = FakeSession()
        self.be = wiley.WileyBackend(cookie="sessionid=abc", user_agent="UA/1",
                                     session=self.session)
        self.be.min_interval = 0

    def test_a_live_session_serves_the_page(self):
        self.session.queue = [(200, PAGE)]
        self.assertIn("pdfdirect", self.be.get(DOI).attachments[0])

    def test_a_live_session_serves_the_pdf(self):
        self.session.queue = [(200, PAGE), PARTICLE]
        self.assertTrue(self.be.get_attachment(DOI).startswith(b"%PDF"))

    def test_the_http_transport_is_never_touched_while_the_session_lives(self):
        self.session.queue = [(200, PAGE), PARTICLE]
        rec = stub(self.be)
        self.be.get_attachment(DOI)
        self.assertEqual(rec.urls, [])

    def test_a_gate_on_the_page_is_still_a_bot_wall(self):
        from litcli.errors import BotWall

        self.session.queue = [(403, "Are you a robot?")]
        with self.assertRaises(BotWall):
            self.be.get(DOI)

    def test_a_404_from_the_session_is_a_not_found_like_the_http_path(self):
        # The cookie path turns any 4xx/5xx into a typed error; a page served by the browser
        # arrives with its status attached and must be judged the same way, or a dead DOI
        # would be parsed as an item with every field empty.
        from litcli.errors import NotFound, Unavailable

        self.session.queue = [(404, "<html>gone</html>")]
        with self.assertRaises(NotFound):
            self.be.get(DOI)
        self.session.queue = [(503, "<html>down</html>")]
        with self.assertRaises(Unavailable):
            self.be.get(DOI)


class FallbackTest(unittest.TestCase):
    def setUp(self):
        self.session = FakeSession()
        self.session.alive = lambda: False
        self.be = wiley.WileyBackend(cookie="sessionid=abc", user_agent="UA/1",
                                     session=self.session)
        self.be.min_interval = 0

    def test_a_dead_session_takes_the_cookie_path(self):
        rec = stub(self.be, (200, PAGE))
        self.be.get(DOI)
        self.assertEqual(len(rec.urls), 1)
        self.assertEqual(self.session.urls, [])
