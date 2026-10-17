"""OpenLibrary book backend offline stub tests: mock urlopen, assert request/parse/error wrapping."""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from litcli.backends.openlibrary import OpenLibraryBackend

DOC = {"key": "/works/OL123W", "title": "Fluid Dynamics",
       "author_name": ["Landau L.", "Lifshitz E."], "first_publish_year": 1959,
       "cover_i": 123456}
DATA = {"docs": [DOC]}


class _Resp:
    def __init__(self, data):
        self._d = data
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def read(self):
        return json.dumps(self._d).encode()


class OpenLibraryStubTest(unittest.TestCase):
    def setUp(self):
        self.be = OpenLibraryBackend()

    def test_search_url_and_parse(self):
        with mock.patch("urllib.request.urlopen", return_value=_Resp(DATA)) as m:
            items = self.be.search("fluid dynamics", limit=2)
        req = m.call_args[0][0]
        self.assertIn("q=fluid+dynamics", req.full_url)
        self.assertIn("limit=2", req.full_url)
        it = items[0]
        self.assertEqual(it.item_type, "book")
        self.assertEqual(it.title, "Fluid Dynamics")
        self.assertEqual(it.year, "1959")
        self.assertEqual(it.key, "/works/OL123W")
        self.assertIn("openlibrary.org/works/OL123W", it.url)
        self.assertEqual(len(it.creators), 2)

    def test_get_works_ends_with_json(self):
        with mock.patch("urllib.request.urlopen", return_value=_Resp(dict(DOC))) as m:
            it = self.be.get("/works/OL123W")
        req = m.call_args[0][0]
        self.assertIn("/works/OL123W.json", req.full_url)
        self.assertEqual(it.title, "Fluid Dynamics")

    def test_404_wrapped(self):
        from urllib.error import HTTPError
        err = HTTPError("u", 404, "Not Found", None, None)
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(RuntimeError) as ctx:
                self.be.get("/works/OL999W")
        self.assertIn("404", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
