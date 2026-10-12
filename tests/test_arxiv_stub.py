'''arXiv 后端离线桩测:patch urlopen,断言请求构造与 Atom 解析,及错误包装。'''
import unittest
import urllib.error
import sys, os
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from litcli.backends.arxiv import ArxivBackend, _short_id

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


class _Resp:
    '''context-manager 化的 urlopen 返回值。'''
    def __init__(self, data):
        self._data = data
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def read(self):
        return self._data


class ArxivStubTest(unittest.TestCase):
    def setUp(self):
        self.be = ArxivBackend(name="arxiv", max_results=5)

    def test_short_id_strips_version(self):
        self.assertEqual(_short_id("http://arxiv.org/abs/2006.12345v1"), "2006.12345")

    def test_search_builds_all_query(self):
        with mock.patch("urllib.request.urlopen", return_value=_Resp(ATOM)) as m:
            items = self.be.search("time evolution", limit=3)
        req = m.call_args[0][0]
        self.assertIn("search_query=all%3Atime+evolution", req.full_url)
        self.assertIn("max_results=3", req.full_url)
        self.assertEqual(items[0].key, "2006.12345")
        self.assertEqual(items[0].title, "Wrinkled Liquid-Drop Impact")
        self.assertEqual(items[0].year, "2020")
        self.assertEqual(items[0].doi, "10.1103/abc")
        self.assertEqual(items[0].creators[0], {"family": "Doe", "given": "Jane"})

    def test_get_builds_id_query(self):
        with mock.patch("urllib.request.urlopen", return_value=_Resp(ATOM)) as m:
            item = self.be.get("2006.12345")
        req = m.call_args[0][0]
        self.assertIn("search_query=id%3A2006.12345", req.full_url)
        self.assertIn("max_results=1", req.full_url)
        self.assertEqual(item.key, "2006.12345")

    def test_search_field_filters_and(self):
        # 字段过滤叠加为 AND 语法:all + ti + au + submittedDate;ti/au 值包引号(多词/逗号作者名不被
        # arXiv 按空白切分而泄成全库 OR)。假定子串(引号按加号、逗号按 %2C 编码)。
        with mock.patch("urllib.request.urlopen", return_value=_Resp(ATOM)) as m:
            self.be.search("finite volume", limit=3,
                           filters={"title": "anisotropic", "author": "Bonaventura",
                                    "year": "2018"})
        req = m.call_args[0][0]
        self.assertIn("search_query=all%3Afinite+volume+AND+ti%3A%22anisotropic%22"
                      "+AND+au%3A%22Bonaventura%22+AND+submittedDate%3A%5B201801010000"
                      "+TO+201812312359%5D", req.full_url)

    def test_search_filters_only_fields(self):
        # 无自由 query 时,仅字段词也组成合法查询
        with mock.patch("urllib.request.urlopen", return_value=_Resp(ATOM)) as m:
            self.be.search("", filters={"title": "mesh"})
        req = m.call_args[0][0]
        self.assertIn("search_query=ti%3A%22mesh%22", req.full_url)
        self.assertTrue(m.call_count == 1)

    def test_http_error_wrapped(self):
        err = urllib.error.HTTPError("http://export.arxiv.org/api/query", 406,
                                     "Not Acceptable", None, None)
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(RuntimeError) as ctx:
                self.be.search("x")
        self.assertIn("406", str(ctx.exception))

    def test_url_error_wrapped(self):
        err = urllib.error.URLError("boom")
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(RuntimeError):
                self.be.search("x")


if __name__ == "__main__":
    unittest.main()
