"""OpenAlex backend offline stub tests: assert request/parse (abstract rebuild/OA PDF)/error wrapping."""
import os
import sys
import unittest
import urllib.parse

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from litcli.backends.openalex import OpenAlexBackend, _inverted_to_text
from litcli.transport import HttpError
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
        self.be.cacheable = False  # tests swap the transport directly, cached bodies would win

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

    def test_an_api_key_rides_the_polite_pool_params(self):
        # The free budget is per source IP and shared by everyone behind it, so a long run can
        # empty it for the whole day; a key makes the request someone's own budget instead.
        be = OpenAlexBackend(mailto="t@e.com", api_key="K1")
        speed_up(be)
        be.cacheable = False
        rec = stub(be, (200, RESULTS))
        be.search("x")
        self.assertIn("api_key=K1", rec.urls[0])
        self.assertIn("mailto=t%40e.com", rec.urls[0])

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

    def test_a_work_without_a_doi_uses_its_w_id_as_the_key(self):
        # Preprints/tech reports carry no DOI; an empty key makes the row uncopyable and
        # related's self-exclusion unreliable. The W-id is resolvable by get().
        rec = dict(WORK, doi=None, id="https://openalex.org/W2088845016")
        stub(self.be, (200, {"results": [rec]}))
        it = self.be.search("deep learning", limit=1)[0]
        self.assertEqual(it.key, "W2088845016")
        self.assertEqual(it.doi, "")

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

    def test_raw_type_and_referenced_works_are_carried_for_scoring(self):
        # The survey scorer ranks by OpenAlex's own type (a "review" outranks an "article") and
        # walks the inline referenced_works, so both must survive as raw data beside the
        # normalized item_type.
        rec = dict(WORK, type="review", referenced_works=["W1", "W2"])
        stub(self.be, (200, {"results": [rec]}))
        it = self.be.search("deep learning", limit=1)[0]
        self.assertEqual(it.extra["openalex_type"], "review")
        self.assertEqual(it.extra["referenced_works"], ["W1", "W2"])
        self.assertEqual(it.item_type, "article")

    def test_referenced_works_absent_is_an_empty_list(self):
        rec = {k: v for k, v in WORK.items() if k != "referenced_works"}
        stub(self.be, (200, {"results": [rec]}))
        it = self.be.search("deep learning", limit=1)[0]
        self.assertEqual(it.extra["referenced_works"], [])

    def test_referenced_works_urls_are_stripped_to_ids(self):
        rec = dict(WORK, referenced_works=["https://openalex.org/W1"])
        stub(self.be, (200, {"results": [rec]}))
        it = self.be.search("deep learning", limit=1)[0]
        self.assertEqual(it.extra["referenced_works"], ["W1"])

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
        # An author with a comma (e.g. "Bonaventura, J.") must be quoted, otherwise OpenAlex splits the
        # filter on the comma -> 400.
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
        # api.openalex.org, returned in inline order; entries the listing does not answer are
        # skipped (the router decides each response, so nothing depends on thread order).
        r1 = {"id": "https://openalex.org/W2088845016", "display_name": "R1",
              "publication_year": 2019, "type": "article", "cited_by_count": 3}
        r2 = {"id": "https://openalex.org/W589102260", "display_name": "R2",
              "publication_year": 2020, "type": "article", "cited_by_count": 1}

        router = _Router({"filter=openalex": {"results": [r1, r2]}}, WORK)  # default: parent work
        self.be._transport = router
        items = self.be.related("10.1038/nature14539", limit=2)
        self.assertEqual(len(items), 2)
        self.assertEqual([it.title for it in items], ["R1", "R2"])
        urls = router.urls
        self.assertIn("/doi:10.1038%2Fnature14539", urls[0])
        batch = next(u for u in urls if "filter=openalex" in u)
        self.assertIn("W2088845016", batch)
        self.assertIn("W589102260", batch)

    def test_references_parallel_and_cited_sorted(self):
        # references: the parent work's inline referenced_works are fetched as one batch
        # and truncated to top-limit by citation count (most important foundations first).
        r1 = {"id": "https://openalex.org/W2088845016", "display_name": "R1",
              "publication_year": 2019, "type": "article", "cited_by_count": 3}
        r2 = {"id": "https://openalex.org/W589102260", "display_name": "R2",
              "publication_year": 2020, "type": "article", "cited_by_count": 1}

        self.be._transport = _Router({"filter=openalex": {"results": [r1, r2]}}, WORK)
        items = self.be.references("10.1038/nature14539", limit=2)
        self.assertEqual([it.title for it in items], ["R1", "R2"])

    def test_references_costs_one_batch_request_not_one_per_work(self):
        # A normal bibliography is 100+ ids; one GET per reference would spend a low effort
        # level on a single row. The ids go out as one filtered listing instead.
        r1 = {"id": "https://openalex.org/W2088845016", "display_name": "R1",
              "publication_year": 2019, "type": "article", "cited_by_count": 3}
        r2 = {"id": "https://openalex.org/W589102260", "display_name": "R2",
              "publication_year": 2020, "type": "article", "cited_by_count": 1}

        router = _Router({"filter=openalex": {"results": [r1, r2]}}, WORK)
        self.be._transport = router
        self.be.references("10.1038/nature14539", limit=2)
        fetches = [u for u in router.urls if "filter=openalex" in u]
        self.assertEqual(len(fetches), 1)

    def test_a_batch_skips_ids_openalex_does_not_return(self):
        # A deleted or merged work simply does not come back; the rest of the batch stands.
        r2 = {"id": "https://openalex.org/W589102260", "display_name": "R2",
              "publication_year": 2020, "type": "article", "cited_by_count": 1}
        router = _Router({"filter=openalex": {"results": [r2]}}, WORK)
        self.be._transport = router
        items = self.be.references("10.1038/nature14539", limit=2)
        self.assertEqual([it.title for it in items], ["R2"])

    def test_a_rate_limited_batch_is_not_an_empty_bibliography(self):
        # Swallowing every batch failure kept one bad batch from costing the others, but a 429
        # then reached the CLI as `[no related]` — the exact shape of "this work cites nothing",
        # so the user retried nothing and the run read a throttled source as wiped.
        class _Throttled(_Router):
            def _handle(self, request):
                if "filter=openalex" in str(request.url):
                    self.urls.append(str(request.url))
                    return httpx.Response(429, text="slow", request=request)
                return super()._handle(request)

        self.be._transport = _Throttled({}, WORK)
        with self.assertRaises(HttpError) as cm:
            self.be.references("10.1038/nature14539", limit=2)
        self.assertEqual(cm.exception.status, 429)

    def test_one_failed_batch_still_lets_the_others_land(self):
        # Only when *nothing* came back does the failure surface: 50 ids of a 60-work
        # bibliography are still a bibliography.
        ids = [f"https://openalex.org/W{i}" for i in range(60)]

        class _Half(httpx.MockTransport):
            def __init__(self):
                self.urls = []
                super().__init__(self._handle)

            def _handle(self, request):
                url = str(request.url)
                self.urls.append(url)
                if "filter=openalex" not in url:
                    return httpx.Response(200, json={**WORK, "referenced_works": ids},
                                          request=request)
                wanted = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["filter"][0]
                if "W59" in wanted:
                    return httpx.Response(429, text="slow", request=request)
                return httpx.Response(200, json={"results": [
                    {"id": f"https://openalex.org/{w}", "display_name": w,
                     "publication_year": 2020} for w in wanted.split(":", 1)[1].split("|")]},
                    request=request)

        self.be._transport = _Half()
        self.assertEqual(len(self.be.references("10.1038/nature14539", limit=60)), 50)

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
        # A source that cannot answer an axis raises instead of returning nothing, so the
        # caller falls through to the next one (the graph commands rely on exactly this).
        from litcli.backends.base import NotSupportedError
        from litcli.backends.crossref import CrossrefBackend
        from litcli.backends.doaj import DoajBackend
        for method in ("references", "citing"):
            with self.assertRaises(NotSupportedError):
                getattr(DoajBackend(), method)("10.1/x", 5)
        # Crossref carries a deposited reference list, never a forward image.
        with self.assertRaises(NotSupportedError):
            CrossrefBackend(mailto="t@e.com").citing("10.1/x", 5)

    def test_5xx_retries_then_success(self):
        rec = stub(self.be, (500, "Server Error"), (200, RESULTS))
        items = self.be.search("x", limit=1)
        self.assertEqual(len(rec.requests), 2)
        self.assertEqual(items[0].title, "Deep learning")

    def test_404_wrapped(self):
        stub(self.be, (404, "Not Found"))
        with self.assertRaises(HttpError) as ctx:
            self.be.get("10.1/nope")
        self.assertEqual(ctx.exception.kind, "not-found")
        self.assertIn("404", str(ctx.exception))

    def _multiref_resp(self, parent):
        low = {"id": "https://openalex.org/W1", "display_name": "Low",
               "publication_year": 2019, "type": "article", "cited_by_count": 1}
        mid = {"id": "https://openalex.org/W2", "display_name": "Mid",
               "publication_year": 2020, "type": "article", "cited_by_count": 5}
        high = {"id": "https://openalex.org/W3", "display_name": "High",
                "publication_year": 2021, "type": "article", "cited_by_count": 100}
        return _Router({"filter=openalex": {"results": [low, mid, high]}}, parent)

    def test_references_fetches_full_set_before_sort(self):
        # references pulls the FULL inline referenced_works, then ranks by citation count, so a
        # highest-cited work sitting past ``limit`` in the unordered inline list still surfaces.
        parent = dict(WORK, referenced_works=[
            "https://openalex.org/W1", "https://openalex.org/W2", "https://openalex.org/W3"])
        router = self._multiref_resp(parent)
        self.be._transport = router
        items = self.be.references("10.1038/nature14539", limit=1)
        self.assertEqual([it.title for it in items], ["High"])
        # All three inline ids go out (not truncated to limit before ranking).
        batch = next(u for u in router.urls if "filter=openalex" in u)
        for wid in ("W1", "W2", "W3"):
            self.assertIn(wid, batch)

    def test_related_natural_order_respects_limit(self):
        # related (no --sort cited): only the first ``limit`` inline neighbors are fetched.
        parent = dict(WORK, related_works=[
            "https://openalex.org/W1", "https://openalex.org/W2", "https://openalex.org/W3"])
        router = self._multiref_resp(parent)
        self.be._transport = router
        items = self.be.related("10.1038/nature14539", limit=1)
        self.assertEqual([it.title for it in items], ["Low"])
        # Only the first neighbor (W1) is asked for when not ranking by citation count.
        batch = next(u for u in router.urls if "filter=openalex" in u)
        self.assertIn("W1", batch)
        self.assertNotIn("W2", batch)
        self.assertNotIn("W3", batch)

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
