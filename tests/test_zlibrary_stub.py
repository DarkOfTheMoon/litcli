"""Z-Library backend offline stub tests: PoW solving, markup parsers, host discovery, download.

The site's HTTP is injected (``_FakeHttp``) and the challenge is exercised through the real
``_Http`` with ``_raw`` patched, so nothing here touches the network. The fixtures mirror the
live 2026-10 markup: a ``Checking your browser`` interstitial carrying an obfuscated constant,
a search page of ``<z-bookcard>`` elements, and a book page with ``bookProperty`` blocks.
"""
import hashlib
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from litcli import config
from litcli.backends import zlibrary
from litcli.backends.zlibrary import (
    ZlibraryBackend,
    book_path,
    challenge_token,
    parse_book_page,
    parse_cards,
)
from litcli.errors import External, Unavailable

# A challenge page whose constant starts with 'F', so the marker sits at digest byte 15
# (the position is the first hex digit — a solver hardcoding byte 0 fails this one).
_CONST = "FB094AF8D70888E6202E3652BA8666334EB393CE"
_CAPTCHA = ("<html><body><p>Checking your browser ...</p><script>"
            f"const a0_0x2a54=['{_CONST}','c_token=','array'];</script></body></html>")

_SEARCH = """
<div id="searchResultBox">
 <div class="book-item resItemBoxBooks">
  <z-bookcard id="118927268" isbn="" href="/book/L85ApO2y8l/some-book.html"
   download="/dl/j04EJpAQxE" deleted="0" publisher="科学出版社" language="中文" year="2024"
   extension="pdf" filesize="19.97 MB" rating="0.0" quality="5">
   <div slot="title">计算流体动力学:基于有限体积法的解析及应用</div>
   <div slot="author">张成春; 沈淳</div>
  </z-bookcard>
 </div>
 <div class="book-item resItemBoxBooks" hidden aria-hidden="true">
  <z-bookcard id="118927268" href="/book/L85ApO2y8l/some-book.html" year="2024">
   <div slot="title">计算流体动力学:基于有限体积法的解析及应用</div>
  </z-bookcard>
 </div>
 <div class="book-item resItemBoxBooks">
  <z-bookcard id="118085450" isbn="9787567218598" href="/book/BEmQB5d5EN/x.html"
   download="/dl/nx8X82gB8x" publisher="苏州：苏州大学出版社" language="chinese" year="2016"
   extension="pdf" rating="4.5">
   <div slot="title">计算流体力学 有限体积法基础及其应用</div>
   <div slot="author">陈丽萍</div>
  </z-bookcard>
 </div>
 <div class="book-item resItemBoxBooks">
  <z-bookcard deleted="1" href="/book/DEADBEEF/x.html"><div slot="title">Deleted</div></z-bookcard>
 </div>
</div>
"""

_BOOK = """
<h1 class="book-title" itemprop="name">计算流体力学 有限体积法基础及其应用</h1>
<i class="authors"><a class='color1' href='/author/A'>陈丽萍</a>, <a href='/author/B'>Liping Chen</a></i>
<a class="btn dlButton addDownloadedBook" href="/dl/nx8X82gB8x" data-book_id="118085450">pdf</a>
<div class="bookDetailsBox">
 <div class='bookProperty property_year'><div class='property_label'>年:</div>
  <div class="property_value">2016</div></div>
 <div class='bookProperty property_edition'><div class='property_label'>出版:</div>
  <div class="property_value">第2版</div></div>
 <div class='bookProperty property_publisher'><div class='property_label'>出版社:</div>
  <div class="property_value">苏州：苏州大学出版社</div></div>
 <div class='bookProperty property_language'><div class='property_label'>语言:</div>
  <span class="property_value text-capitalize">中文</span></div>
 <div class='bookProperty property_pages'><div class='property_label'>页:</div>
  <div class="property_value"><span title="Pages paperback">262</span></div></div>
 <div class='bookProperty property_isbn 10'><div class='property_label'>ISBN 10:</div>
  <div class="property_value">7567218593</div></div>
 <div class='bookProperty property_isbn 13'><div class='property_label'>ISBN 13:</div>
  <div class="property_value">9787567218598</div></div>
</div>
<script type="application/ld+json">{"@type": "Book", "name": "计算流体力学",
 "description": "1 (p1): 第1章 绪论 &lt;br&gt;2 (p1-1): 1-1 概述", "numberOfPages": 0}</script>
"""

_BOOK_URL = "https://zl.test/book/BEmQB5d5EN"
_DL_URL = "https://zl.test/dl/nx8X82gB8x"


class _FakeHttp:
    """Minimal stand-in for ``zlibrary._Http``: serves per-URL bodies and records calls."""

    def __init__(self, pages=None, fail=(), empty=()):
        self.pages = pages or {}
        self.fail = set(fail)
        self.empty = set(empty)
        self.calls = []

    def fetch(self, url, referer=""):
        self.calls.append((url, referer))
        if url in self.fail:
            raise RuntimeError("boom")
        if url in self.empty:
            return url, 204, b""
        body = self.pages.get(url)
        if body is None:
            return url, 404, b""
        if isinstance(body, list):
            body = body.pop(0) if body else ""
        return url, 200, body.encode() if isinstance(body, str) else body


def _backend(empty=(), **pages):
    """A backend pinned to a base URL, fed by a fake HTTP layer."""
    http = _FakeHttp(pages, empty=empty)

    class _Pinned(ZlibraryBackend):
        def _http(self):
            return http

    return _Pinned(base="https://zl.test"), http


class ChallengeTest(unittest.TestCase):
    def test_solves_marker_at_digest_byte_15(self):
        token = challenge_token(_CAPTCHA)
        self.assertTrue(token.startswith(_CONST))
        n = int(token[len(_CONST):])
        digest = hashlib.sha1(token.encode()).digest()
        self.assertEqual((digest[15], digest[16]), (0xB0, 0x0B))
        self.assertGreater(n, 0)

    def test_solves_marker_at_byte_0(self):
        const = "008B946335513B019274C040BC7DB56CCA23620D"
        page = f"a0_0x2a54=['{const}','c_token=','array']"
        digest = b"\xb0\x0b"
        token = challenge_token(page)
        self.assertTrue(hashlib.sha1(token.encode()).digest().startswith(digest))

    def test_absent_marker_is_empty(self):
        self.assertEqual(challenge_token("<html>normal page</html>"), "")

    def test_http_retries_once_after_solving(self):
        http = zlibrary._Http(delay=0)
        solved = challenge_token(_CAPTCHA)
        with mock.patch.object(http, "_raw", side_effect=[
                ("https://zl.test/s/x", 503, _CAPTCHA.encode()),
                ("https://zl.test/s/x", 200, b"<html>ok</html>")]) as raw:
            _final, status, body = http.fetch("https://zl.test/s/x")
        self.assertEqual((status, body), (200, b"<html>ok</html>"))
        self.assertEqual(raw.call_count, 2)
        cookie = {c.name: c.value for c in http._client.cookies.jar}
        self.assertEqual(cookie["c_token"], solved)
        self.assertIn("c_time", cookie)

    def test_http_passes_through_when_not_challenged(self):
        http = zlibrary._Http(delay=0)
        with mock.patch.object(http, "_raw", return_value=("https://zl.test/s/x", 200, b"ok")) as raw:
            self.assertEqual(http.fetch("https://zl.test/s/x")[1], 200)
        self.assertEqual(raw.call_count, 1)


class BookPathTest(unittest.TestCase):
    def test_normalizes_all_key_forms(self):
        self.assertEqual(book_path("zlibrary/book/L85ApO2y8l/slug.html"), "book/L85ApO2y8l")
        self.assertEqual(book_path("https://zh.test/book/L85ApO2y8l/slug.html"), "book/L85ApO2y8l")
        self.assertEqual(book_path("book/L85ApO2y8l"), "book/L85ApO2y8l")
        self.assertEqual(book_path("L85ApO2y8l"), "book/L85ApO2y8l")

    def test_rejects_non_book_key(self):
        with self.assertRaises(KeyError):
            book_path("finite volume method")


class ParseCardsTest(unittest.TestCase):
    def test_extracts_fields_and_dedups(self):
        items = parse_cards(_SEARCH)
        self.assertEqual(len(items), 2)  # the repeated hidden card and the deleted one drop out
        first = items[0]
        self.assertEqual(first.key, "book/L85ApO2y8l")
        self.assertEqual(first.title, "计算流体动力学:基于有限体积法的解析及应用")
        self.assertEqual(first.creators, [{"family": "张成春", "given": ""},
                                          {"family": "沈淳", "given": ""}])
        self.assertEqual(first.year, "2024")
        self.assertEqual(first.publisher, "科学出版社")
        self.assertEqual(first.item_type, "book")
        self.assertEqual(first.extra["download"], "/dl/j04EJpAQxE")
        self.assertEqual(items[1].isbn, "9787567218598")

    def test_empty_title_is_skipped(self):
        page = '<z-bookcard href="/book/AAA111/x.html"></z-bookcard>'
        self.assertEqual(parse_cards(page), [])


class ParseBookPageTest(unittest.TestCase):
    def test_extracts_record(self):
        record = parse_book_page(_BOOK)
        self.assertEqual(record["title"], "计算流体力学 有限体积法基础及其应用")
        self.assertEqual(record["authors"], ["陈丽萍", "Liping Chen"])
        self.assertEqual(record["year"], "2016")
        self.assertEqual(record["edition"], "第2版")
        self.assertEqual(record["publisher"], "苏州大学出版社")  # city prefix dropped
        self.assertEqual(record["pages"], "262")
        self.assertEqual(record["isbn"], "9787567218598")  # ISBN-13 wins over ISBN-10
        self.assertEqual(record["download"], "/dl/nx8X82gB8x")
        self.assertIn("第1章", record["description"])  # JSON-LD description, tags unescaped

    def test_zero_pages_stays_empty(self):
        page = "<h1 class='book-title'>T</h1>" + _BOOK.split("</div>", 1)[0]
        self.assertEqual(parse_book_page(page)["pages"], "")  # numberOfPages 0 never renders "0"


class BackendTest(unittest.TestCase):
    def test_read_only_capabilities(self):
        self.assertEqual(sorted(ZlibraryBackend().capabilities), ["read"])

    def test_search_maps_cards_and_applies_limit(self):
        search = "https://zl.test/s/%E6%9C%89%E9%99%90%E4%BD%93%E7%A7%AF%E6%B3%95"
        backend, http = _backend(**{search: _SEARCH})
        with mock.patch.object(ZlibraryBackend, "_resolve_base", return_value="https://zl.test"):
            items = backend.search("有限体积法", limit=1)
        self.assertEqual(len(items), 1)
        self.assertEqual(http.calls[0][0], search)

    def test_search_passes_year_filter_and_post_filters_title(self):
        backend, http = _backend(**{"https://zl.test/s/q?yearFrom=2024&yearTo=2024": _SEARCH})
        with mock.patch.object(ZlibraryBackend, "_resolve_base", return_value="https://zl.test"):
            items = backend.search("q", filters={"year": "2024", "title": "流体力学"})
        self.assertTrue(http.calls[0][0].endswith("yearFrom=2024&yearTo=2024"))
        self.assertEqual([i.key for i in items], ["book/BEmQB5d5EN"])

    def test_search_blank_query_returns_empty(self):
        backend, http = _backend()
        self.assertEqual(backend.search("  "), [])
        self.assertEqual(http.calls, [])

    def test_get_builds_full_item(self):
        backend, _ = _backend(**{_BOOK_URL: _BOOK})
        with mock.patch.object(ZlibraryBackend, "_resolve_base", return_value="https://zl.test"):
            item = backend.get("zlibrary/book/BEmQB5d5EN/x.html")
        self.assertEqual(item.key, "book/BEmQB5d5EN")
        self.assertEqual(item.isbn, "9787567218598")
        self.assertEqual(item.edition, "第2版")
        self.assertEqual(item.publisher, "苏州大学出版社")
        self.assertEqual(item.page, "262")
        self.assertEqual(item.url, _BOOK_URL)
        self.assertEqual(item.extra["language"], "中文")

    def test_get_missing_book_raises_keyerror(self):
        backend, _ = _backend()
        with mock.patch.object(ZlibraryBackend, "_resolve_base", return_value="https://zl.test"), \
                self.assertRaises(KeyError):
            backend.get("book/NOPE123/x.html")

    def test_get_attachment_downloads_pdf(self):
        backend, _http = _backend(**{_BOOK_URL: _BOOK, _DL_URL: b"%PDF-1.7 bytes"})
        with mock.patch.object(ZlibraryBackend, "_resolve_base", return_value="https://zl.test"):
            self.assertEqual(backend.get_attachment("book/BEmQB5d5EN/x.html"), b"%PDF-1.7 bytes")

    def test_unavailable_page_204_reports_actionable_error(self):
        backend, _ = _backend(empty={_BOOK_URL})
        with mock.patch.object(ZlibraryBackend, "_resolve_base", return_value="https://zl.test"), \
                self.assertRaises(Unavailable) as ctx:
            backend.get("book/BEmQB5d5EN/x.html")
        self.assertIn("HTTP 204", str(ctx.exception))
        self.assertIn("another candidate", str(ctx.exception))

    def test_get_attachment_204_reports_limit(self):
        backend, _ = _backend(empty={_DL_URL}, **{_BOOK_URL: _BOOK})
        with mock.patch.object(ZlibraryBackend, "_resolve_base", return_value="https://zl.test"), \
                self.assertRaises(Unavailable) as ctx:
            backend.get_attachment("book/BEmQB5d5EN/x.html")
        self.assertIn("daily download limit", str(ctx.exception))

    def test_get_attachment_rejects_non_pdf(self):
        backend, _ = _backend(**{_BOOK_URL: _BOOK, _DL_URL: b"<html>login</html>"})
        with mock.patch.object(ZlibraryBackend, "_resolve_base", return_value="https://zl.test"), \
                self.assertRaises(External):
            backend.get_attachment("book/BEmQB5d5EN/x.html")


class BaseDiscoveryTest(unittest.TestCase):
    def test_discovers_host_from_redirect(self):
        backend = ZlibraryBackend(mirrors=["entry.test"])
        http = _FakeHttp()
        http.fetch = lambda url, referer="": (url, 200, b"<html>home</html>") \
            if url == "https://entry.test" else ("https://zh.test/", 200, b"home")
        self.assertEqual(backend._resolve_base(http), "https://entry.test")
        self.assertEqual(backend._base, "https://entry.test")

    def test_unreachable_mirrors_raise(self):
        backend = ZlibraryBackend(mirrors=["a.test", "b.test"])
        with self.assertRaises(Unavailable):
            backend._resolve_base(_FakeHttp(fail={"https://a.test/", "https://b.test/"}))

    def test_configured_base_skips_discovery(self):
        backend = ZlibraryBackend(base="https://zl.test/")
        self.assertEqual(backend._resolve_base(_FakeHttp()), "https://zl.test")


class RegisterTest(unittest.TestCase):
    def test_discover_zlibrary_read_only(self):
        backends = config.discover_backends({"backends": {"zlibrary": {"type": "zlibrary"}}})
        self.assertEqual(sorted(backends["zlibrary"].capabilities), ["read"])


if __name__ == "__main__":
    unittest.main()
