"""Douban backend offline stub tests: pure parsers, register, and Playwright adapter mocks.

Playwright is a heavy optional dependency, so these tests mock the browser (sync_api ->
launch_persistent_context) rather than drive a real one. The pure parsers
(window_data_to_items / _parse_info / _subject_to_item) operate on plain payloads and are
browser-free. Payloads below mirror the live SSR window.__DATA__ schema (see the ground-truth
probe in the session summary).
"""
import sys
import os
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from litcli.backends.douban import (
    DoubanBackend,
    window_data_to_items,
    _parse_info,
    _subject_to_item,
)
from litcli import config


# --- pure parsers (browser-free) ---

# A realistic search-card payload from the live window.__DATA__.
_SAMPLE_PAYLOAD = {
    "count": 15, "error_info": "", "total": 15, "start": 0,
    "items": [
        {
            "abstract": "李人宪 / 国防工业出版社 / 2008-3 / 22.00元",
            "id": 3061999, "title": "有限体积法基础",
            "url": "https://book.douban.com/subject/3061999/",
            "rating": {"count": 0, "star_count": 0, "value": 0, "rating_info": "评价人数不足"},
        },
        {
            "abstract": "修正小杰 / 电子工业出版社 / 2021-1 / 89.00元",
            "id": 35040389, "title": "有限体积法及其应用",
            "url": "https://book.douban.com/subject/35040389/",
            "rating": {"count": 12, "star_count": 3, "value": 8.5, "rating_info": "9人评价"},
        },
        {"title": "", "id": 9999999, "url": "https://book.douban.com/subject/9999999/"},
        None,  # non-dict cards are tolerated
    ],
}


class DoubanParseTest(unittest.TestCase):
    def test_window_data_to_items_extracts_fields(self):
        items = window_data_to_items(_SAMPLE_PAYLOAD)
        # 2 valid cards (empty-title card and None skipped)
        self.assertEqual(len(items), 2)
        it = items[0]
        self.assertEqual(it.source, "douban")
        self.assertEqual(it.item_type, "book")
        self.assertEqual(it.key, "3061999")
        self.assertEqual(it.title, "有限体积法基础")
        self.assertEqual(it.publisher, "国防工业出版社")
        self.assertEqual(it.year, "2008")
        self.assertEqual(it.url, "https://book.douban.com/subject/3061999/")
        self.assertEqual(it.creators[0]["family"], "李人宪")
        self.assertIn("douban", it.extra)
        self.assertIn("douban_rating", it.extra)

    def test_window_data_to_items_tolerates_non_dict(self):
        self.assertEqual(window_data_to_items([]), [])
        self.assertEqual(window_data_to_items(None), [])
        self.assertEqual(window_data_to_items("not a dict"), [])

    def test_parse_info_newline_separated(self):
        # Live #info renders block-level (innerText uses '\n' between label/value pairs).
        info = _parse_info("作者: 李人宪\n出版社: 国防工业出版社\n出版年: 2008-3\nISBN: 9787118055559\n页数: 226\n定价: 22.00元\n")
        self.assertEqual(info["author"], "李人宪")
        self.assertEqual(info["publisher"], "国防工业出版社")
        self.assertEqual(info["pubdate"], "2008-3")
        self.assertEqual(info["isbn"], "9787118055559")
        self.assertEqual(info["pages"], "226")
        self.assertEqual(info["price"], "22.00元")

    def test_parse_info_fullwidth_colon_and_missing(self):
        info = _parse_info("作者：王安石／编委会\n出版社：人民文学出版社")
        self.assertEqual(info["author"], "王安石／编委会")
        self.assertEqual(info["publisher"], "人民文学出版社")
        self.assertEqual(info["isbn"], "")  # missing key -> empty, no crash

    def test_subject_to_item_builds_book(self):
        info = {"author": "李人宪 / 某某", "publisher": "国防工业出版社",
                "pubdate": "2008-3", "isbn": "9787118055559", "pages": "226", "price": "22.00元"}
        it = _subject_to_item("有限体积法基础", "https://book.douban.com/subject/3061999/",
                              "3061999", info)
        self.assertEqual(it.item_type, "book")
        self.assertEqual(it.key, "3061999")
        self.assertEqual(it.isbn, "9787118055559")
        self.assertEqual(it.year, "2008")
        self.assertEqual(it.page, "226")
        self.assertEqual([c["family"] for c in it.creators], ["李人宪", "某某"])


# --- backend register ---

class DoubanRegisterTest(unittest.TestCase):
    def test_discover_douban_read_only(self):
        backends = config.discover_backends({"backends": {"douban": {"type": "douban"}}})
        self.assertEqual(backends["douban"].name, "douban")
        self.assertEqual(sorted(backends["douban"].capabilities), ["read"])


# --- Playwright adapter (mocked browser, no network) ---

class _FakePage:
    """Fake Playwright page carrying a configurable window.__DATA__ / #info."""

    def __init__(self, url="https://search.douban.com/book/subject_search", payload=None, node=None):
        self.url = url
        self._payload = payload
        self._node = node

    def evaluate(self, expr):
        # "() => window.__DATA__ ?? null"
        return self._payload

    def eval_on_selector(self, sel, expr):
        if self._node is None:
            return ""
        if sel == "#wrapper h1 span":
            return self._node.get("title", "")
        if sel == "#info":
            return self._node.get("info", "")
        return ""


class DoubanAdapterTest(unittest.TestCase):
    def test_detect_captcha_default_false(self):
        # Douban has no captcha; the base default must be False even on an odd URL.
        be = DoubanBackend()
        self.assertFalse(be._detect_captcha(_FakePage(url="https://book.douban.com/")))

    def test_read_payload_parses_json_string(self):
        import json
        be = DoubanBackend()
        page = _FakePage(payload=json.dumps({"items": []}))
        self.assertEqual(be._read_payload(page), {"items": []})

    def test_subject_url_normalizes(self):
        be = DoubanBackend()
        self.assertEqual(be._subject_url("3061999"), "https://book.douban.com/subject/3061999/")
        self.assertEqual(be._subject_url("subject/3061999"), "https://book.douban.com/subject/3061999/")
        # A search result is keyed douban/<id>; this must route directly to the subject page.
        self.assertEqual(be._subject_url("douban/3061999"), "https://book.douban.com/subject/3061999/")
        self.assertEqual(be._subject_url("https://book.douban.com/subject/3061999/"),
                         "https://book.douban.com/subject/3061999/")
        with self.assertRaises(KeyError):
            be._subject_url("not-a-subject")

    def test_missing_playwright_raises_clear_message(self):
        db = DoubanBackend()
        with mock.patch.dict(sys.modules, {"playwright.sync_api": None}):
            with self.assertRaises(RuntimeError) as ctx:
                db._playwright()
        self.assertIn("Playwright is not installed", str(ctx.exception))
        self.assertIn("--with-douban", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
