"""Crossref backend offline stub tests: assert request construction / parsing / type mapping / 5xx retry / error wrapping."""
import os
import sys
import unittest


sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from litcli.backends.crossref import CrossrefBackend
from tests.http_stub import speed_up, stub

REC = {"title": ["Deep learning"],
       "author": [{"given": "Yann", "family": "LeCun"}, {"given": "Yoshua", "family": "Bengio"}],
       "published": {"date-parts": [[2015]]},
       "DOI": "10.1038/nature14539", "type": "journal-article",
       "URL": "http://dx.doi.org/10.1038/nature14539",
       "container-title": ["Nature"], "volume": "521", "issue": "7553",
       "page": "436-444", "publisher": "Nature Publishing Group",
       "is-referenced-by-count": 84467,
       "abstract": "<jats:p>We review deep learning.</jats:p>"}
MSG = {"message": {"items": [REC], "total-results": 1}}


class CrossrefStubTest(unittest.TestCase):
    def setUp(self):
        self.be = CrossrefBackend(mailto="t@e.com")
        speed_up(self.be)

    def test_search_url_and_parse(self):
        rec = stub(self.be, (200, MSG))
        items = self.be.search("deep learning", limit=3)
        self.assertIn("query.bibliographic=deep+learning", rec.urls[0])
        self.assertIn("rows=3", rec.urls[0])
        it = items[0]
        self.assertEqual(it.key, "10.1038/nature14539")
        self.assertEqual(it.title, "Deep learning")
        self.assertEqual(it.year, "2015")
        self.assertEqual(it.item_type, "article")
        self.assertEqual(it.creators[0], {"family": "LeCun", "given": "Yann"})
        self.assertNotIn("jats", it.abstract.lower())  # tags stripped

    def test_type_map_book(self):
        rec = dict(REC); rec["type"] = "book"; rec["title"] = ["A Book"]
        stub(self.be, (200, {"message": rec}))
        it = self.be.get("10.1/book")
        self.assertEqual(it.item_type, "book")

    def test_type_map_patent(self):
        rec = dict(REC); rec["type"] = "patent"; rec["title"] = ["A Patent"]
        stub(self.be, (200, {"message": rec}))
        it = self.be.get("10.1/pat")
        self.assertEqual(it.item_type, "patent")

    def test_get_doi_encoded(self):
        rec = stub(self.be, (200, MSG))
        self.be.get("10.1038/nature14539")
        self.assertEqual(rec.urls[0], "https://api.crossref.org/works/10.1038%2Fnature14539")

    def test_container_volume_page_populated(self):
        stub(self.be, (200, MSG))
        it = self.be.search("deep learning", limit=1)[0]
        self.assertEqual(it.container_title, "Nature")
        self.assertEqual(it.volume, "521")
        self.assertEqual(it.issue, "7553")
        self.assertEqual(it.page, "436-444")
        self.assertEqual(it.publisher, "Nature Publishing Group")

    def test_cited_by_count_parsed(self):
        stub(self.be, (200, MSG))
        it = self.be.search("deep learning", limit=1)[0]
        self.assertEqual(it.cited_by_count, 84467)
        self.assertIn("is-referenced-by-count", REC)

    def test_search_field_filters_url(self):
        # Field filters map to query.title/author/container-title + filter year range
        rec = stub(self.be, (200, MSG))
        self.be.search("anisotropic", limit=3,
                       filters={"title": "anisotropic", "author": "Bonaventura",
                                "container": "JCP", "year": "2018"})
        url = rec.urls[0]
        self.assertIn("query.bibliographic=anisotropic", url)
        self.assertIn("query.title=anisotropic", url)
        self.assertIn("query.author=Bonaventura", url)
        self.assertIn("query.container-title=JCP", url)
        self.assertIn("filter=from-pub-date%3A2018-01-01%2Cuntil-pub-date%3A2018-12-31", url)
        self.assertIn("rows=3", url)

    def test_5xx_retries_then_success(self):
        rec = stub(self.be, (500, "Server Error"), (200, MSG))
        items = self.be.search("x", limit=1)
        self.assertEqual(len(rec.requests), 2)
        self.assertEqual(items[0].title, "Deep learning")

    def test_404_wrapped(self):
        stub(self.be, (404, "Not Found"))
        with self.assertRaises(RuntimeError) as ctx:
            self.be.get("10.1/nope")
        self.assertIn("404", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
