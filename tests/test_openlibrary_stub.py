"""OpenLibrary book backend offline stub tests: assert request/parse/error wrapping."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from litcli.backends.openlibrary import OpenLibraryBackend
from litcli.transport import HttpError
from tests.http_stub import stub

DOC = {"key": "/works/OL123W", "title": "Fluid Dynamics",
       "author_name": ["Landau L.", "Lifshitz E."], "first_publish_year": 1959,
       "cover_i": 123456}
DATA = {"docs": [DOC]}


class OpenLibraryStubTest(unittest.TestCase):
    def setUp(self):
        self.be = OpenLibraryBackend()

    def test_search_url_and_parse(self):
        rec = stub(self.be, (200, DATA))
        items = self.be.search("fluid dynamics", limit=2)
        self.assertIn("q=fluid+dynamics", rec.urls[0])
        self.assertIn("limit=2", rec.urls[0])
        it = items[0]
        self.assertEqual(it.item_type, "book")
        self.assertEqual(it.title, "Fluid Dynamics")
        self.assertEqual(it.year, "1959")
        self.assertEqual(it.key, "/works/OL123W")
        self.assertIn("openlibrary.org/works/OL123W", it.url)
        self.assertEqual(len(it.creators), 2)

    def test_get_works_ends_with_json(self):
        rec = stub(self.be, (200, dict(DOC)))
        it = self.be.get("/works/OL123W")
        self.assertIn("/works/OL123W.json", rec.urls[0])
        self.assertEqual(it.title, "Fluid Dynamics")

    def test_404_wrapped(self):
        stub(self.be, (404, "Not Found"))
        with self.assertRaises(HttpError) as ctx:
            self.be.get("/works/OL999W")
        self.assertEqual(ctx.exception.kind, "not-found")
        self.assertIn("404", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
