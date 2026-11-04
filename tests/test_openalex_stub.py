"""OpenAlex backend offline stub tests: assert request/parse (abstract rebuild/OA PDF)/error wrapping."""
import os
import sys
import unittest

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from litcli.backends.openalex import OpenAlexBackend, _inverted_to_text
from tests.http_stub import speed_up, stub

WORK = {
    "id": "https://openalex.org/W2919115771",
    "display_name": "Deep learning",
    "doi": "https://doi.org/10.1038/nature14539",
    "publication_year": 2015,
    "type": "article",
    "authorships": [{"author": {"display_name": "Yann LeCun"}},
                    {"author": {"display_name": "Yoshua Bengio"}}],
    "abstract_inverted_index": {"We": ["0"], "review": ["1"], "deep": ["2"], "learning": ["3"]},
    "best_oa_location": {"pdf_url": "http://oa.example/x.pdf"},
    "primary_location": {"source": {"display_name": "Nature"}},
    "biblio": {"volume": "521", "issue": "7553", "first_page": "436", "last_page": "444"},
    "publisher": "Nature Publishing Group",
    "cited_by_count": 84467,
    "related_works": ["https://openalex.org/W2088845016", "https://openalex.org/W589102260"],
    "referenced_works": ["https://openalex.org/W2088845016", "https://openalex.org/W589102260"],
}
RESULTS = {"results": [WORK]}


class _Router(httpx.MockTransport):
    """Answers by URL substring, because the fan-out fetches arrive in any order."""

    def __init__(self, table: dict, default: dict):
        self.urls: list[str] = []
        self._table, self._default = table, default
        super().__init__(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.urls.append(str(request.url))
        for marker, payload in self._table.items():
            if marker in str(request.url):
                return httpx.Response(200, json=payload, request=request)
        return httpx.Response(200, json=self._default, request=request)


class OpenAlexStubTest(unittest.TestCase):
    def setUp(self):
        self.be = OpenAlexBackend(mailto="t@e.com")
        speed_up(self.be)

    def test_inverted_rebuild(self):
        self.assertEqual(_inverted_to_text(WORK["abstract_inverted_index"]),
                         "We review deep learning")

    def test_search_url_and_parse(self):
        rec = stub(self.be, (200, RESULTS))
        items = self.be.search("deep learning", limit=3)
        self.assertIn("search=deep+learning", rec.urls[0])
        self.assertIn("per_page=3", rec.urls[0])
        it = items[0]
        self.assertEqual(it.title, "Deep learning")
        self.assertEqual(it.year, "2015")
        self.assertEqual(it.item_type, "article")
        self.assertEqual(it.creators[0], {"family": "LeCun", "given": "Yann"})
        self.assertEqual(it.abstract, "We review deep learning")
        self.assertEqual(it.attachments, ["http://oa.example/x.pdf"])

    def test_get_doi_encoded(self):
        rec = stub(self.be, (200, WORK))
        self.be.get("10.1038/nature14539")
        self.assertIn("/doi:10.1038%2Fnature14539", rec.urls[0])

    def test_doi_normalized_to_bare(self):
        # OpenAlex's doi is a full URL; it should be stored as a bare DOI (for identity keys / export standard)
        stub(self.be, (200, RESULTS))
        it = self.be.search("deep learning", limit=1)[0]
        self.assertEqual(it.doi, "10.1038/nature14539")
        self.assertNotIn("https://doi.org/", it.doi)

    def test_url_is_real_landing_not_bare_doi(self):
        # url must be a resolvable URL (consistent with other backends), not a bare DOI string.
        stub(self.be, (200, RESULTS))
        it = self.be.search("deep learning", limit=1)[0]
        self.assertEqual(it.url, "https://doi.org/10.1038/nature14539")

    def test_url_falls_back_to_oa_landing_when_no_doi(self):
        no_doi = dict(WORK, doi="")
        no_doi["best_oa_location"]["landing_page_url"] = "https://example.com/landing"
        stub(self.be, (200, {"results": [no_doi]}))
        it = self.be.search("deep learning", limit=1)[0]
        self.assertEqual(it.url, "https://example.com/landing")

    def test_container_volume_page_populated(self):
        stub(self.be, (200, RESULTS))
        it = self.be.search("deep learning", limit=1)[0]
        self.assertEqual(it.container_title, "Nature")
        self.assertEqual(it.volume, "521")
        self.assertEqual(it.issue, "7553")
        self.assertEqual(it.page, "436-444")
        self.assertEqual(it.publisher, "Nature Publishing Group")

    def test_cited_and_openalex_id_parsed(self):
        stub(self.be, (200, WORK))
        it = self.be.get("10.1038/nature14539")
        self.assertEqual(it.cited_by_count, 84467)
        self.assertEqual(it.extra["openalex_id"], "https://openalex.org/W2919115771")

    def test_search_field_filters_url(self):
        # Field map: filter=title.search,raw_author_name.search,publication_year; values quoted (comma-safe);
        # container folds into search. Filter values are quoted so an author name containing a comma is not
        # split by OpenAlex into a 400.
        rec = stub(self.be, (200, RESULTS))
        self.be.search("finite volume", limit=3,
                       filters={"title": "anisotropic", "author": "Bonaventura",
                                "year": "2018", "container": "JCP"})
        url = rec.urls[0]
        self.assertIn("search=finite+volume+JCP", url)
        self.assertIn("per_page=3", url)
        self.assertIn("filter=title.search%3A%22anisotropic%22%2C"
                      "raw_author_name.search%3A%22Bonaventura%22%2Cpublication_year%3A2018", url)
        self.assertIn("mailto=t%40e.com", url)

    def test_search_filter_comma_quoted(self):
        # An author with a comma (e.g. "Bonaventura, J.") must be quoted, otherwise OpenAlex splits the filter on the comma -> 400.
        rec = stub(self.be, (200, RESULTS))
        self.be.search("turbulence", limit=3, filters={"author": "Bonaventura, J."})
        self.assertIn('raw_author_name.search%3A%22Bonaventura%2C+J.%22', rec.urls[0])

    def test_search_filters_year_not_int_crash(self):
        # Passing an int year no longer triggers an .isdigit() AttributeError (_filters uniformly str-ifies).
        rec = stub(self.be, (200, RESULTS))
        self.be.search("mesh", limit=3, filters={"year": 2018})
        self.assertIn("publication_year%3A2018", rec.urls[0])

    def test_related_returns_related_works(self):
        # related: fetch the parent work once (inline related_works, W ids), then fetch each one's own
        # api.openalex.org URL in parallel, returned in inline order; failed entries are skipped (here the
        # URL decides each response to avoid thread-scheduling ambiguity).
        r1 = {"id": "https://openalex.org/W2088845016", "display_name": "R1",
              "publication_year": 2019, "type": "article", "cited_by_count": 3}
        r2 = {"id": "https://openalex.org/W589102260", "display_name": "R2",
              "publication_year": 2020, "type": "article", "cited_by_count": 1}

        router = _Router({"W2088845016": r1, "W589102260": r2}, WORK)  # default: parent work
        self.be._transport = router
        items = self.be.related("10.1038/nature14539", limit=2)
        self.assertEqual(len(items), 2)
        self.assertEqual([it.title for it in items], ["R1", "R2"])
        urls = router.urls
        self.assertIn("/doi:10.1038%2Fnature14539", urls[0])
        self.assertTrue(any("/works/W2088845016" in u for u in urls))
        self.assertTrue(any("/works/W589102260" in u for u in urls))

    def test_references_parallel_and_cited_sorted(self):
        # references: parent work's inline referenced_works are fetched in parallel and
        # truncated to top-limit by citation count (most important foundations first).
        r1 = {"id": "https://openalex.org/W2088845016", "display_name": "R1",
              "publication_year": 2019, "type": "article", "cited_by_count": 3}
        r2 = {"id": "https://openalex.org/W589102260", "display_name": "R2",
              "publication_year": 2020, "type": "article", "cited_by_count": 1}

        self.be._transport = _Router({"W2088845016": r1, "W589102260": r2}, WORK)
        items = self.be.references("10.1038/nature14539", limit=2)
        self.assertEqual([it.title for it in items], ["R1", "R2"])

    def test_citing_uses_cites_filter_sorted(self):
        # citing: single ``filter=cites:<wid>&sort=cited_by_count:desc`` request, no per-work fetch.
        r1 = {"id": "https://openalex.org/W2088845016", "display_name": "C1",
              "publication_year": 2016, "type": "article", "cited_by_count": 900}
        r2 = {"id": "https://openalex.org/W589102260", "display_name": "C2",
              "publication_year": 2017, "type": "article", "cited_by_count": 100}

        router = _Router({"filter=cites": {"results": [r1, r2]}}, WORK)
        self.be._transport = router
        items = self.be.citing("10.1038/nature14539", limit=2)
        self.assertEqual(len(items), 2)
        self.assertEqual([it.title for it in items], ["C1", "C2"])
        cite_url = next(u for u in router.urls if "filter=cites" in u)
        self.assertIn("filter=cites:W2919115771", cite_url)
        self.assertIn("sort=cited_by_count:desc", cite_url)

    def test_references_not_supported_on_other_backends(self):
        # Non-openalex read-only backends raise NotSupportedError for references/citing.
        from litcli.backends.crossref import CrossrefBackend
        from litcli.backends.base import NotSupportedError
        be = CrossrefBackend(mailto="t@e.com")
        for method in ("references", "citing"):
            with self.assertRaises(NotSupportedError):
                getattr(be, method)("10.1/x", 5)

    def test_5xx_retries_then_success(self):
        rec = stub(self.be, (500, "Server Error"), (200, RESULTS))
        items = self.be.search("x", limit=1)
        self.assertEqual(len(rec.requests), 2)
        self.assertEqual(items[0].title, "Deep learning")

    def test_404_wrapped(self):
        stub(self.be, (404, "Not Found"))
        with self.assertRaises(RuntimeError) as ctx:
            self.be.get("10.1/nope")
        self.assertIn("404", str(ctx.exception))

    def _multiref_resp(self, parent):
        low = {"id": "https://openalex.org/W1", "display_name": "Low",
               "publication_year": 2019, "type": "article", "cited_by_count": 1}
        mid = {"id": "https://openalex.org/W2", "display_name": "Mid",
               "publication_year": 2020, "type": "article", "cited_by_count": 5}
        high = {"id": "https://openalex.org/W3", "display_name": "High",
                "publication_year": 2021, "type": "article", "cited_by_count": 100}
        return _Router({"W1": low, "W2": mid, "W3": high}, parent)

    def test_references_fetches_full_set_before_sort(self):
        # references pulls the FULL inline referenced_works, then ranks by citation count, so a
        # highest-cited work sitting past ``limit`` in the unordered inline list still surfaces.
        parent = dict(WORK, referenced_works=[
            "https://openalex.org/W1", "https://openalex.org/W2", "https://openalex.org/W3"])
        router = self._multiref_resp(parent)
        self.be._transport = router
        items = self.be.references("10.1038/nature14539", limit=1)
        self.assertEqual([it.title for it in items], ["High"])
        # All three inline ids are fetched (not truncated to limit before ranking).
        fetched = set(router.urls)
        for url in fetched:
            self.assertTrue("/W1" in url or "/W2" in url or "/W3" in url or "doi:" in url,
                            f"unexpected URL {url}")

    def test_related_natural_order_respects_limit(self):
        # related (no --sort cited): only the first ``limit`` inline neighbors are fetched.
        parent = dict(WORK, related_works=[
            "https://openalex.org/W1", "https://openalex.org/W2", "https://openalex.org/W3"])
        router = self._multiref_resp(parent)
        self.be._transport = router
        items = self.be.related("10.1038/nature14539", limit=1)
        self.assertEqual([it.title for it in items], ["Low"])
        fetched = router.urls
        # Only the first neighbor (W1) is fetched when not ranking by citation count.
        self.assertFalse(any("/W2" in u or "/W3" in u for u in fetched))

    def test_citing_caps_per_page(self):
        # OpenAlex per_page is bounded to [1, 200]; avoid a 400 on --limit 250.
        router = _Router({"filter=cites": {"results": [WORK]}}, WORK)
        self.be._transport = router
        self.be.citing("10.1038/nature14539", limit=250)
        cite_url = next(u for u in router.urls if "filter=cites" in u)
        self.assertIn("per_page=200", cite_url)

    def test_citing_zero_limit_no_400(self):
        router = _Router({"filter=cites": {"results": [WORK]}}, WORK)
        self.be._transport = router
        items = self.be.citing("10.1038/nature14539", limit=0)
        self.assertEqual(items, [])
        cite_url = next(u for u in router.urls if "filter=cites" in u)
        self.assertIn("per_page=1", cite_url)


if __name__ == "__main__":
    unittest.main()
