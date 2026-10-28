"""Zenodo backend offline stub tests: type mapping, PDF attachment, key forms."""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from litcli.backends.zenodo import ZenodoBackend, record_to_item

RECORD = {
    "id": 13770811, "doi": "10.5281/zenodo.13770811",
    "metadata": {
        "title": "Método de volúmenes finitos",
        "publication_date": "2024-09-17",
        "resource_type": {"type": "dataset"},
        "license": {"id": "cc-by-4.0"},
        "creators": [{"name": "García Gavilánez, Robinson José"}],
        "description": "<p>Descripción &amp; notas</p>",
        "access_right": "open",
    },
    "links": {"self_html": "https://zenodo.org/records/13770811"},
    "files": [{"key": "slides.pdf", "mimetype": None,
               "links": {"self": "https://zenodo.org/api/records/13770811/files/slides.pdf/content"}},
              {"key": "data.zip", "mimetype": None,
               "links": {"self": "https://zenodo.org/api/records/13770811/files/data.zip/content"}}],
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


class ZenodoStubTest(unittest.TestCase):
    def setUp(self):
        self.be = ZenodoBackend(delay=0)

    def test_record_maps_type_abstract_and_pdf(self):
        it = record_to_item(RECORD)
        self.assertEqual(it.item_type, "dataset")
        self.assertEqual(it.abstract, "Descripción & notas")
        self.assertEqual(it.year, "2024")
        self.assertEqual(it.creators, [{"family": "García Gavilánez", "given": "Robinson José"}])
        self.assertEqual(it.attachments,
                         ["https://zenodo.org/api/records/13770811/files/slides.pdf/content"])
        self.assertEqual(it.extra["license"], "cc-by-4.0")

    def test_record_without_pdf_has_no_attachment(self):
        no_pdf = dict(RECORD, files=[{"key": "data.zip",
                                      "links": {"self": "https://x.test/data.zip/content"}}])
        self.assertEqual(record_to_item(no_pdf).attachments, [])

    def test_search_uses_field_queries_and_year_post_filter(self):
        with mock.patch("urllib.request.urlopen",
                        return_value=_Resp({"hits": {"hits": [RECORD]}})) as m:
            items = self.be.search("volúmenes", filters={"year": "2024", "author": "García"})
        url = m.call_args[0][0].full_url
        self.assertIn("creators.name%3A%22Garc%C3%ADa%22", url)
        self.assertEqual(len(items), 1)
        with mock.patch("urllib.request.urlopen",
                        return_value=_Resp({"hits": {"hits": [RECORD]}})):
            self.assertEqual(self.be.search("volúmenes", filters={"year": "1999"}), [])

    def test_search_never_asks_for_more_than_the_anonymous_page_cap(self):
        with mock.patch("urllib.request.urlopen", return_value=_Resp({"hits": {"hits": []}})) as m:
            self.be.search("volúmenes", limit=100)
        self.assertIn("size=25", m.call_args[0][0].full_url)

    def test_search_pages_past_the_anonymous_cap(self):
        pages = [{"hits": {"hits": [RECORD] * 25}}, {"hits": {"hits": [RECORD] * 5}}]
        with mock.patch("urllib.request.urlopen",
                        side_effect=[_Resp(p) for p in pages]) as m:
            items = self.be.search("volúmenes", limit=30)
        self.assertEqual(len(items), 30)
        self.assertEqual(m.call_count, 2)
        self.assertIn("page=2", m.call_args_list[1][0][0].full_url)

    def test_get_accepts_a_record_url(self):
        for url in ("https://zenodo.org/records/13770811",
                    "https://zenodo.org/api/records/13770811"):
            with mock.patch("urllib.request.urlopen", return_value=_Resp(RECORD)) as m:
                it = self.be.get(url)
            self.assertTrue(m.call_args[0][0].full_url.endswith("/13770811"))
            self.assertEqual(it.doi, "10.5281/zenodo.13770811")

    def test_search_pushes_the_year_into_the_query(self):
        with mock.patch("urllib.request.urlopen", return_value=_Resp({"hits": {"hits": [RECORD]}})) as m:
            self.be.search("volúmenes", filters={"year": "2017"})
        url = m.call_args[0][0].full_url
        self.assertIn("publication_date", url)
        self.assertIn("2017-01-01", url)
        self.assertEqual(m.call_count, 1)

    def test_get_by_numeric_id(self):
        with mock.patch("urllib.request.urlopen", return_value=_Resp(RECORD)) as m:
            it = self.be.get("zenodo/13770811")
        self.assertTrue(m.call_args[0][0].full_url.endswith("/13770811"))
        self.assertEqual(it.doi, "10.5281/zenodo.13770811")

    def test_get_by_doi_derives_the_record_id(self):
        with mock.patch("urllib.request.urlopen", return_value=_Resp(RECORD)) as m:
            self.be.get("10.5281/zenodo.13770811")
        self.assertTrue(m.call_args[0][0].full_url.endswith("/13770811"))

    def test_get_unknown_key_raises(self):
        with self.assertRaises(KeyError):
            self.be.get("finite volume")


if __name__ == "__main__":
    unittest.main()
