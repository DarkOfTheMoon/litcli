"""DOAJ backend offline stub tests: mock urlopen, assert query shape and normalization."""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from litcli.backends.doaj import DoajBackend, record_to_item

RECORD = {
    "bibjson": {
        "title": "Numerical analysis of flow and heat transfer enhancement",
        "author": [{"name": "Azher M. Abed"}, {"name": "张成春"}],
        "year": "2018",
        "month": "09",
        "journal": {"title": "Case Studies in Thermal Engineering", "volume": "12",
                    "number": "3", "publisher": "Elsevier", "issns": ["2214-157X"]},
        "start_page": "101", "end_page": "110",
        "identifier": [{"id": "2214-157X", "type": "pissn"},
                       {"id": "10.1016/j.csite.2018.10.004", "type": "doi"}],
        "link": [{"type": "fulltext", "url": "https://example.test/article"}],
        "abstract": "A finite volume study.",
        "subject": [{"code": "TA1", "term": "Engineering"}],
    }
}


class _Resp:
    def __init__(self, data):
        self._d = data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps(self._d).encode()


class DoajStubTest(unittest.TestCase):
    def setUp(self):
        self.be = DoajBackend(delay=0)

    def test_search_builds_field_query_and_parses(self):
        with mock.patch("urllib.request.urlopen",
                        return_value=_Resp({"total": 1, "results": [RECORD]})) as m:
            items = self.be.search("heat transfer", limit=2,
                                   filters={"year": "2018", "title": "flow"})
        url = m.call_args[0][0].full_url
        self.assertIn("pageSize=2", url)
        self.assertIn("bibjson.title%3A%22flow%22", url)
        self.assertIn("bibjson.year%3A2018", url)
        it = items[0]
        self.assertEqual(it.item_type, "article")
        self.assertEqual(it.doi, "10.1016/j.csite.2018.10.004")
        self.assertEqual(it.year, "2018")
        self.assertEqual(it.container_title, "Case Studies in Thermal Engineering")
        self.assertEqual(it.page, "101-110")
        self.assertEqual(it.url, "https://example.test/article")
        self.assertEqual(it.creators[1], {"family": "张成春", "given": ""})

    def test_search_without_terms_queries_everything(self):
        with mock.patch("urllib.request.urlopen", return_value=_Resp({"results": []})) as m:
            self.assertEqual(self.be.search(""), [])
        # A bare "*" is accepted; the "*:*" form is rejected by the live API (400).
        self.assertIn("/%2A?pageSize=", m.call_args[0][0].full_url)

    def test_get_by_doi_raises_when_missing(self):
        with mock.patch("urllib.request.urlopen", return_value=_Resp({"results": []})):
            with self.assertRaises(KeyError):
                self.be.get("10.1000/nope")

    def test_get_by_fulltext_url_uses_the_link_field(self):
        # A record without a DOI is keyed by its fulltext URL; get must resolve that form.
        with mock.patch("urllib.request.urlopen", return_value=_Resp({"results": [RECORD]})) as m:
            it = self.be.get("doaj/https://example.test/article")
        self.assertIn("bibjson.link.url%3A%22https%3A%2F%2Fexample.test%2Farticle%22",
                      m.call_args[0][0].full_url)
        self.assertEqual(it.doi, "10.1016/j.csite.2018.10.004")

    def test_search_paginates_past_the_api_page_cap(self):
        pages = [{"results": [RECORD] * 100}, {"results": [RECORD] * 50}]
        with mock.patch("urllib.request.urlopen",
                        side_effect=[_Resp(p) for p in pages]) as m:
            items = self.be.search("heat", limit=150)
        self.assertEqual(len(items), 150)
        self.assertEqual(m.call_count, 2)
        self.assertIn("page=2", m.call_args_list[1][0][0].full_url)

    def test_get_keeps_a_doi_org_occurrence_inside_a_link_url(self):
        key = "https://example.test/redirect?to=https://doi.org/10.1/x"
        with mock.patch("urllib.request.urlopen", return_value=_Resp({"results": [RECORD]})) as m:
            self.be.get(key)
        url = m.call_args[0][0].full_url
        self.assertIn("bibjson.link.url", url)
        self.assertIn("doi.org%2F10.1%2Fx", url)  # prefix-only strip keeps the embedded DOI

    def test_get_escapes_a_doi_with_specials(self):
        with mock.patch("urllib.request.urlopen", return_value=_Resp({"results": [RECORD]})) as m:
            self.be.get("10.1/x?y")
        self.assertIn("%5C%3Fy", m.call_args[0][0].full_url)

    def test_get_by_url_with_query_string_escapes_lucene_specials(self):
        # "?"/"&" unescaped make DOAJ answer 400 "disallowed Lucene features".
        with mock.patch("urllib.request.urlopen", return_value=_Resp({"results": [RECORD]})) as m:
            self.be.get("http://js.test/thesis?columnId=1&index=0")
        url = m.call_args[0][0].full_url
        self.assertIn("%5C%3FcolumnId", url)
        self.assertIn("%5C%26index", url)

    def test_record_without_doi_falls_back_to_doi_url(self):
        bare = {"title": "T", "journal": {"title": "J"}}
        self.assertEqual(record_to_item(bare).url, "")
        with_doi = {"title": "T", "identifier": [{"id": "10.1/x", "type": "doi"}]}
        self.assertEqual(record_to_item(with_doi).url, "https://doi.org/10.1/x")


if __name__ == "__main__":
    unittest.main()
