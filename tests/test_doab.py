"""DOAB: a bot check answered with a 200 must not be served from the metadata cache.

The record pages run through ``fetch_text``, which caches successful bodies for a day. Anubis
serves its check with a 200, so a cached check would keep every later read failing for the rest
of the TTL.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from litcli import cache
from litcli.backends.doab import DoabBackend
from tests.http_stub import speed_up, stub

HANDLE = "20.500.12854/57156"
URL = f"https://directory.doabooks.org/handle/{HANDLE}"

COOKIE = {"Set-Cookie": "techaro.lol-anubis-auth=v; Domain=.doabooks.org; Path=/"}

PAGE = """<html><head><title>Book Title</title>
<meta name="DC.title" content="Book Title" /></head><body>
<div class="page-header first-page-header"><h2>Book Title</h2></div>
<div class="item-page-field-wrapper"><h5>ISBN</h5>9789535104452</div>
</body></html>"""


def _challenge() -> str:
    body = {"rules": {"difficulty": 0}, "challenge": {"id": "cid-1", "randomData": "abc123"}}
    return ('<html><script id="anubis_challenge" type="application/json">'
            + json.dumps(body) + "</script></html>")


class CachedChallengeTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp()
        mock.patch("litcli.cache.cache_dir", return_value=Path(self._dir)).start()
        self.addCleanup(mock.patch.stopall)
        self.be = DoabBackend(delay=0)
        speed_up(self.be)

    def _prime_cache(self, body: str) -> None:
        cache.put(self.be._cache_key(URL), body)

    def test_a_cached_200_challenge_is_retried_past_the_cache(self):
        self._prime_cache(_challenge())
        rec = stub(self.be, (200, "", COOKIE), (200, PAGE))
        self.be.cacheable = True  # ``stub`` turns it off; this test is about the cache
        self.assertEqual(self.be.get(HANDLE).title, "Book Title")
        # The check was answered, then the page refetched rather than replayed from the cache.
        self.assertIn("api/pass-challenge", rec.urls[0])
        self.assertEqual(rec.urls[1], URL)

    def test_a_plain_cached_page_is_still_served_from_the_cache(self):
        self._prime_cache(PAGE)
        rec = stub(self.be, (200, "fresh"))
        self.be.cacheable = True
        self.assertEqual(self.be.get(HANDLE).title, "Book Title")
        self.assertEqual(rec.urls, [])

    def test_a_fresh_200_challenge_is_answered_and_the_page_replaces_it_in_the_cache(self):
        rec = stub(self.be, (200, _challenge()), (200, "", COOKIE), (200, PAGE))
        self.be.cacheable = True
        self.assertEqual(self.be.get(HANDLE).title, "Book Title")
        self.assertEqual(rec.urls[-1], URL)
        # The real page was stored over the check, so the next read is a plain cache hit.
        self.assertEqual(self.be.get(HANDLE).title, "Book Title")
        self.assertEqual(len(rec.urls), 3)


if __name__ == "__main__":
    unittest.main()
