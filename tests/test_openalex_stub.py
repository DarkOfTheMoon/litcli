"""OpenAlex 后端离线桩测:mock urlopen,断言请求/解析(摘要重建/OA PDF)/错误包装。"""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from litcli.backends.openalex import OpenAlexBackend, _inverted_to_text

WORK = {
    "display_name": "Deep learning",
    "doi": "https://doi.org/10.1038/nature14539",
    "publication_year": 2015,
    "type": "article",
    "authorships": [{"author": {"display_name": "Yann LeCun"}},
                    {"author": {"display_name": "Yoshua Bengio"}}],
    "abstract_inverted_index": {"We": ["0"], "review": ["1"], "deep": ["2"], "learning": ["3"]},
    "best_oa_location": {"pdf_url": "http://oa.example/x.pdf"},
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


if __name__ == "__main__":
    unittest.main()
