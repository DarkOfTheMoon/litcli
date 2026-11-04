'''arXiv backend offline stub tests: assert request construction and Atom parsing, and error wrapping.'''
import unittest
import httpx
import sys, os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from litcli.backends.arxiv import ArxivBackend, _short_id
from tests.http_stub import error, speed_up, stub

ATOM = b'''<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2006.12345v1</id>
    <updated>2020-06-15T00:00:00Z</updated>
    <published>2020-06-14T00:00:00Z</published>
    <title>  Wrinkled  Liquid-Drop Impact  </title>
    <summary>  A study of drop impact.  </summary>
    <author><name>Jane Doe</name></author>
    <author><name>Bob Smith</name></author>
    <arxiv:doi>10.1103/abc</arxiv:doi>
  </entry>
</feed>'''


class ArxivStubTest(unittest.TestCase):
    def setUp(self):
        self.be = ArxivBackend(name="arxiv", max_results=5)
        speed_up(self.be)

    def test_short_id_strips_version(self):
        self.assertEqual(_short_id("http://arxiv.org/abs/2006.12345v1"), "2006.12345")

    def test_search_builds_all_query(self):
        rec = stub(self.be, (200, ATOM))
        items = self.be.search("time evolution", limit=3)
        self.assertIn("search_query=all%3Atime+evolution", rec.urls[0])
        self.assertIn("max_results=3", rec.urls[0])
        self.assertEqual(items[0].key, "2006.12345")
        self.assertEqual(items[0].title, "Wrinkled Liquid-Drop Impact")
        self.assertEqual(items[0].year, "2020")
        self.assertEqual(items[0].doi, "10.1103/abc")
        self.assertEqual(items[0].creators[0], {"family": "Doe", "given": "Jane"})

    def test_get_builds_id_query(self):
        rec = stub(self.be, (200, ATOM))
        item = self.be.get("2006.12345")
        self.assertIn("search_query=id%3A2006.12345", rec.urls[0])
        self.assertIn("max_results=1", rec.urls[0])
        self.assertEqual(item.key, "2006.12345")

    def test_search_field_filters_and(self):
        # Field filters compose into AND syntax: all + ti + au + submittedDate; ti/au values are quoted
        # (multi-word / comma author names are not split on whitespace by arXiv into a full-library OR).
        # Assumed substrings (quotes as plus, commas as %2C).
        rec = stub(self.be, (200, ATOM))
        self.be.search("finite volume", limit=3,
                       filters={"title": "anisotropic", "author": "Bonaventura",
                                "year": "2018"})
        self.assertIn("search_query=all%3Afinite+volume+AND+ti%3A%22anisotropic%22"
                      "+AND+au%3A%22Bonaventura%22+AND+submittedDate%3A%5B201801010000"
                      "+TO+201812312359%5D", rec.urls[0])

    def test_search_filters_only_fields(self):
        # With no free query, field-only words still form a valid query
        rec = stub(self.be, (200, ATOM))
        self.be.search("", filters={"title": "mesh"})
        self.assertIn("search_query=ti%3A%22mesh%22", rec.urls[0])
        self.assertEqual(len(rec.requests), 1)

    def test_http_error_wrapped(self):
        stub(self.be, (406, "Not Acceptable"))
        with self.assertRaises(RuntimeError) as ctx:
            self.be.search("x")
        self.assertIn("406", str(ctx.exception))

    def test_transport_error_wrapped(self):
        self.be._transport = error(httpx.ConnectError("boom"))
        with self.assertRaises(RuntimeError):
            self.be.search("x")

    def test_429_retries_then_succeeds(self):
        # A rate-limit HTTP 429 must back off and retry, then succeed on a later attempt.
        rec = stub(self.be, (429, "Rate exceeded."), (200, ATOM))
        items = self.be.search("x")
        self.assertEqual(items[0].key, "2006.12345")
        self.assertEqual(len(rec.requests), 2)

    def test_429_exhausted_raises_clear_message(self):
        rec = stub(self.be, (429, "Rate exceeded."))
        with self.assertRaises(RuntimeError) as ctx:
            self.be.search("x")
        self.assertIn("arXiv rate-limited", str(ctx.exception))
        self.assertEqual(len(rec.requests), 3)  # all retry attempts used

    def test_ua_is_descriptive_and_contacts(self):
        # arXiv expects a descriptive UA; mailto joins the polite pool and must be in the header.
        be = ArxivBackend(name="arxiv", mailto="me@example.com")
        rec = stub(be, (200, ATOM))
        be.search("x")
        ua = rec.header("user-agent")
        self.assertIn("litcli/0.1", ua)
        self.assertIn("me@example.com", ua)


if __name__ == "__main__":
    unittest.main()
