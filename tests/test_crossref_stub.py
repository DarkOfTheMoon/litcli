"""Crossref 后端离线桩测:mock urlopen,断言请求构造/解析/类型映射/5xx 重试/错误包装。"""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from litcli.backends.crossref import CrossrefBackend

REC = {"title": ["Deep learning"],
       "author": [{"given": "Yann", "family": "LeCun"}, {"given": "Yoshua", "family": "Bengio"}],
       "published": {"date-parts": [[2015]]},
       "DOI": "10.1038/nature14539", "type": "journal-article",
       "URL": "http://dx.doi.org/10.1038/nature14539",
       "abstract": "<jats:p>We review deep learning.</jats:p>"}
MSG = {"message": {"items": [REC], "total-results": 1}}


class _Resp:
    def __init__(self, data):
        self._d = data
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def read(self):
        return json.dumps(self._d).encode()


class CrossrefStubTest(unittest.TestCase):
    def setUp(self):
        self.be = CrossrefBackend(mailto="t@e.com")

    def test_search_url_and_parse(self):
        with mock.patch("urllib.request.urlopen", return_value=_Resp(MSG)) as m:
            items = self.be.search("deep learning", limit=3)
        req = m.call_args[0][0]
        self.assertIn("query.bibliographic=deep+learning", req.full_url)
        self.assertIn("rows=3", req.full_url)
        it = items[0]
        self.assertEqual(it.key, "10.1038/nature14539")
        self.assertEqual(it.title, "Deep learning")
        self.assertEqual(it.year, "2015")
        self.assertEqual(it.item_type, "article")
        self.assertEqual(it.creators[0], {"family": "LeCun", "given": "Yann"})
        self.assertNotIn("jats", it.abstract.lower())  # 已去标签

    def test_type_map_book(self):
        rec = dict(REC); rec["type"] = "book"; rec["title"] = ["A Book"]
        with mock.patch("urllib.request.urlopen", return_value=_Resp({"message": rec})):
            it = self.be.get("10.1/book")
        self.assertEqual(it.item_type, "book")

    def test_type_map_patent(self):
        rec = dict(REC); rec["type"] = "patent"; rec["title"] = ["A Patent"]
        with mock.patch("urllib.request.urlopen", return_value=_Resp({"message": rec})):
            it = self.be.get("10.1/pat")
        self.assertEqual(it.item_type, "patent")

    def test_get_doi_encoded(self):
        with mock.patch("urllib.request.urlopen", return_value=_Resp(MSG)) as m:
            self.be.get("10.1038/nature14539")
        req = m.call_args[0][0]
        self.assertEqual(req.full_url, "https://api.crossref.org/works/10.1038%2Fnature14539")

    def test_5xx_retries_then_success(self):
        from urllib.error import HTTPError
        err = HTTPError("u", 500, "Server Error", None, None)
        with mock.patch("urllib.request.urlopen", side_effect=[err, _Resp(MSG)]) as m:
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
