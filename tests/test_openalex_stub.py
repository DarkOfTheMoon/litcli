"""OpenAlex backend offline stub tests: mock urlopen, assert request/parse (abstract rebuild/OA PDF)/error wrapping."""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from litcli.backends.openalex import OpenAlexBackend, _inverted_to_text

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


class _Resp:
    def __init__(self, data):
        self._d = data
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def read(self):
        return json.dumps(self._d).encode()


class OpenAlexStubTest(unittest.TestCase):
    def setUp(self):
        self.be = OpenAlexBackend(mailto="t@e.com")

    def test_inverted_rebuild(self):
        self.assertEqual(_inverted_to_text(WORK["abstract_inverted_index"]),
                         "We review deep learning")

    def test_search_url_and_parse(self):
        with mock.patch("urllib.request.urlopen", return_value=_Resp(RESULTS)) as m:
            items = self.be.search("deep learning", limit=3)
        req = m.call_args[0][0]
        self.assertIn("search=deep+learning", req.full_url)
        self.assertIn("per_page=3", req.full_url)
        it = items[0]
        self.assertEqual(it.title, "Deep learning")
        self.assertEqual(it.year, "2015")
        self.assertEqual(it.item_type, "article")
        self.assertEqual(it.creators[0], {"family": "LeCun", "given": "Yann"})
        self.assertEqual(it.abstract, "We review deep learning")
        self.assertEqual(it.attachments, ["http://oa.example/x.pdf"])

    def test_get_doi_encoded(self):
        with mock.patch("urllib.request.urlopen", return_value=_Resp(WORK)) as m:
            self.be.get("10.1038/nature14539")
        req = m.call_args[0][0]
        self.assertIn("/doi:10.1038%2Fnature14539", req.full_url)

    def test_doi_normalized_to_bare(self):
        # OpenAlex's doi is a full URL; it should be stored as a bare DOI (for identity keys / export standard)
        with mock.patch("urllib.request.urlopen", return_value=_Resp(RESULTS)):
            it = self.be.search("deep learning", limit=1)[0]
        self.assertEqual(it.doi, "10.1038/nature14539")
        self.assertNotIn("https://doi.org/", it.doi)

    def test_container_volume_page_populated(self):
        with mock.patch("urllib.request.urlopen", return_value=_Resp(RESULTS)):
            it = self.be.search("deep learning", limit=1)[0]
        self.assertEqual(it.container_title, "Nature")
        self.assertEqual(it.volume, "521")
        self.assertEqual(it.issue, "7553")
        self.assertEqual(it.page, "436-444")
        self.assertEqual(it.publisher, "Nature Publishing Group")

    def test_cited_and_openalex_id_parsed(self):
        with mock.patch("urllib.request.urlopen", return_value=_Resp(WORK)):
            it = self.be.get("10.1038/nature14539")
        self.assertEqual(it.cited_by_count, 84467)
        self.assertEqual(it.extra["openalex_id"], "https://openalex.org/W2919115771")

    def test_search_field_filters_url(self):
        # Field map: filter=title.search,raw_author_name.search,publication_year; values quoted (comma-safe);
        # container folds into search. Filter values are quoted so an author name containing a comma is not
        # split by OpenAlex into a 400.
        with mock.patch("urllib.request.urlopen", return_value=_Resp(RESULTS)) as m:
            self.be.search("finite volume", limit=3,
                           filters={"title": "anisotropic", "author": "Bonaventura",
                                    "year": "2018", "container": "JCP"})
        req = m.call_args[0][0]
        self.assertIn("search=finite+volume+JCP", req.full_url)
        self.assertIn("per_page=3", req.full_url)
        self.assertIn("filter=title.search%3A%22anisotropic%22%2C"
                      "raw_author_name.search%3A%22Bonaventura%22%2Cpublication_year%3A2018", req.full_url)
        self.assertIn("mailto=t%40e.com", req.full_url)

    def test_search_filter_comma_quoted(self):
        # An author with a comma (e.g. "Bonaventura, J.") must be quoted, otherwise OpenAlex splits the filter on the comma -> 400.
        with mock.patch("urllib.request.urlopen", return_value=_Resp(RESULTS)) as m:
            self.be.search("turbulence", limit=3, filters={"author": "Bonaventura, J."})
        req = m.call_args[0][0]
        self.assertIn('raw_author_name.search%3A%22Bonaventura%2C+J.%22', req.full_url)

    def test_search_filters_year_not_int_crash(self):
        # Passing an int year no longer triggers an .isdigit() AttributeError (_filters uniformly str-ifies).
        with mock.patch("urllib.request.urlopen", return_value=_Resp(RESULTS)) as m:
            self.be.search("mesh", limit=3, filters={"year": 2018})
        req = m.call_args[0][0]
        self.assertIn("publication_year%3A2018", req.full_url)

    def test_related_returns_related_works(self):
        # related: fetch the parent work once (inline related_works, W ids), then fetch each one's own
        # api.openalex.org URL in parallel, returned in inline order; failed entries are skipped (here the
        # URL decides each response to avoid thread-scheduling ambiguity).
        r1 = {"id": "https://openalex.org/W2088845016", "display_name": "R1",
              "publication_year": 2019, "type": "article", "cited_by_count": 3}
        r2 = {"id": "https://openalex.org/W589102260", "display_name": "R2",
              "publication_year": 2020, "type": "article", "cited_by_count": 1}

        from urllib.request import Request
        def _resp_for(req: Request, *a, **kw):
            u = req.full_url
            if "W2088845016" in u:
                return _Resp(r1)
            if "W589102260" in u:
                return _Resp(r2)
            return _Resp(WORK)  # parent work (/doi:...)

        with mock.patch("urllib.request.urlopen", side_effect=_resp_for) as m:
            items = self.be.related("10.1038/nature14539", limit=2)
        self.assertEqual(len(items), 2)
        self.assertEqual([it.title for it in items], ["R1", "R2"])
        urls = [m.call_args_list[i][0][0].full_url for i in range(m.call_count)]
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

        from urllib.request import Request
        def _resp_for(req: Request, *a, **kw):
            u = req.full_url
            if "W2088845016" in u:
                return _Resp(r1)
            if "W589102260" in u:
                return _Resp(r2)
            return _Resp(WORK)

        with mock.patch("urllib.request.urlopen", side_effect=_resp_for) as m:
            items = self.be.references("10.1038/nature14539", limit=2)
        self.assertEqual([it.title for it in items], ["R1", "R2"])

    def test_citing_uses_cites_filter_sorted(self):
        # citing: single ``filter=cites:<wid>&sort=cited_by_count:desc`` request, no per-work fetch.
        r1 = {"id": "https://openalex.org/W2088845016", "display_name": "C1",
              "publication_year": 2016, "type": "article", "cited_by_count": 900}
        r2 = {"id": "https://openalex.org/W589102260", "display_name": "C2",
              "publication_year": 2017, "type": "article", "cited_by_count": 100}

        from urllib.request import Request
        def _resp_for(req: Request, *a, **kw):
            u = req.full_url
            if "filter=cites" in u:
                return _Resp({"results": [r1, r2]})
            return _Resp(WORK)

        with mock.patch("urllib.request.urlopen", side_effect=_resp_for) as m:
            items = self.be.citing("10.1038/nature14539", limit=2)
        self.assertEqual(len(items), 2)
        self.assertEqual([it.title for it in items], ["C1", "C2"])
        cite_url = next(m.call_args_list[i][0][0].full_url
                        for i in range(m.call_count) if "filter=cites" in m.call_args_list[i][0][0].full_url)
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
        from urllib.error import HTTPError
        err = HTTPError("u", 500, "Server Error", None, None)
        with mock.patch("urllib.request.urlopen", side_effect=[err, _Resp(RESULTS)]) as m:
            items = self.be.search("x", limit=1)
        self.assertEqual(m.call_count, 2)
        self.assertEqual(items[0].title, "Deep learning")

    def test_404_wrapped(self):
        from urllib.error import HTTPError
        err = HTTPError("u", 404, "Not Found", None, None)
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(RuntimeError) as ctx:
                self.be.get("10.1/nope")
        self.assertIn("404", str(ctx.exception))

    def _multiref_resp(self, parent):
        from urllib.request import Request

        low = {"id": "https://openalex.org/W1", "display_name": "Low",
               "publication_year": 2019, "type": "article", "cited_by_count": 1}
        mid = {"id": "https://openalex.org/W2", "display_name": "Mid",
               "publication_year": 2020, "type": "article", "cited_by_count": 5}
        high = {"id": "https://openalex.org/W3", "display_name": "High",
                "publication_year": 2021, "type": "article", "cited_by_count": 100}

        def _resp_for(req: Request, *a, **kw):
            u = req.full_url
            if "/W1" in u:
                return _Resp(low)
            if "/W2" in u:
                return _Resp(mid)
            if "/W3" in u:
                return _Resp(high)
            return _Resp(parent)

        return _resp_for, {"W1": low, "W2": mid, "W3": high}

    def test_references_fetches_full_set_before_sort(self):
        # references pulls the FULL inline referenced_works, then ranks by citation count, so a
        # highest-cited work sitting past ``limit`` in the unordered inline list still surfaces.
        parent = dict(WORK, referenced_works=[
            "https://openalex.org/W1", "https://openalex.org/W2", "https://openalex.org/W3"])
        _resp_for, _ = self._multiref_resp(parent)
        with mock.patch("urllib.request.urlopen", side_effect=_resp_for) as m:
            items = self.be.references("10.1038/nature14539", limit=1)
        self.assertEqual([it.title for it in items], ["High"])
        # All three inline ids are fetched (not truncated to limit before ranking).
        fetched = set(call[0][0].full_url for call in m.call_args_list)
        for url in fetched:
            self.assertTrue("/W1" in url or "/W2" in url or "/W3" in url or "doi:" in url,
                            f"unexpected URL {url}")

    def test_related_natural_order_respects_limit(self):
        # related (no --sort cited): only the first ``limit`` inline neighbors are fetched.
        parent = dict(WORK, related_works=[
            "https://openalex.org/W1", "https://openalex.org/W2", "https://openalex.org/W3"])
        _resp_for, _ = self._multiref_resp(parent)
        with mock.patch("urllib.request.urlopen", side_effect=_resp_for) as m:
            items = self.be.related("10.1038/nature14539", limit=1)
        self.assertEqual([it.title for it in items], ["Low"])
        fetched = [call[0][0].full_url for call in m.call_args_list]
        # Only the first neighbor (W1) is fetched when not ranking by citation count.
        self.assertFalse(any("/W2" in u or "/W3" in u for u in fetched))

    def test_citing_caps_per_page(self):
        # OpenAlex per_page is bounded to [1, 200]; avoid a 400 on --limit 250.
        from urllib.request import Request

        def _resp_for(req: Request, *a, **kw):
            if "filter=cites" in req.full_url:
                return _Resp({"results": [WORK]})
            return _Resp(WORK)

        with mock.patch("urllib.request.urlopen", side_effect=_resp_for) as m:
            self.be.citing("10.1038/nature14539", limit=250)
        cite_url = next(m.call_args_list[i][0][0].full_url
                        for i in range(m.call_count) if "filter=cites" in m.call_args_list[i][0][0].full_url)
        self.assertIn("per_page=200", cite_url)

    def test_citing_zero_limit_no_400(self):
        from urllib.request import Request

        def _resp_for(req: Request, *a, **kw):
            if "filter=cites" in req.full_url:
                return _Resp({"results": [WORK]})
            return _Resp(WORK)

        with mock.patch("urllib.request.urlopen", side_effect=_resp_for) as m:
            items = self.be.citing("10.1038/nature14539", limit=0)
        self.assertEqual(items, [])
        cite_url = next(m.call_args_list[i][0][0].full_url
                        for i in range(m.call_count) if "filter=cites" in m.call_args_list[i][0][0].full_url)
        self.assertIn("per_page=1", cite_url)


if __name__ == "__main__":
    unittest.main()
