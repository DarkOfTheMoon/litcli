"""Unpaywall 解析器离线桩测:mock urlopen,断言 URL/解析/错误包装。"""
import json
import os
import sys
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from litcli import unpaywall

DATA = {"is_oa": True, "oa_status": "gold", "title": "Deep learning", "year": 2015,
        "best_oa_location": {"url_for_pdf": "http://oa.example/p.pdf",
                             "url_for_landing_page": "http://oa.example/landing"}}


class _Resp:
    def __init__(self, data):
        self._d = data
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def read(self):
        return json.dumps(self._d).encode()


class UnpaywallStubTest(unittest.TestCase):
    def test_resolve_url_and_parse(self):
        with mock.patch("urllib.request.urlopen", return_value=_Resp(DATA)) as m:
            info = unpaywall.resolve_oa("10.1038/nature14539", "t@e.com")
        req = m.call_args[0][0]
        self.assertIn("10.1038%2Fnature14539", req.full_url)
        self.assertIn("email=t%40e.com", req.full_url)
        self.assertTrue(info["is_oa"])
        self.assertEqual(info["oa_status"], "gold")
        self.assertEqual(info["best_pdf"], "http://oa.example/p.pdf")
        self.assertEqual(info["title"], "Deep learning")

    def test_no_oa(self):
        no = dict(DATA); no["is_oa"] = False; no["best_oa_location"] = None
        with mock.patch("urllib.request.urlopen", return_value=_Resp(no)):
            info = unpaywall.resolve_oa("10.1/x", "t@e.com")
        self.assertFalse(info["is_oa"])
        self.assertEqual(info["best_pdf"], "")

    def test_422_wrapped(self):
        from urllib.error import HTTPError
        err = HTTPError("u", 422, "Unprocessable", None, None)
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(RuntimeError) as ctx:
                unpaywall.resolve_oa("10.1/x", "bad")
        self.assertIn("422", str(ctx.exception))

    def test_url_error_wrapped(self):
        err = urllib.error.URLError("boom")
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(RuntimeError):
                unpaywall.resolve_oa("10.1/x", "t@e.com")


if __name__ == "__main__":
    unittest.main()
