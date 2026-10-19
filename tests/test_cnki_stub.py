"""CNKI backend offline stub tests: pure parser, register, and Playwright adapter mocks.

Playwright is a heavy optional dependency and the sandbox may not reach CNKI over the
network; these tests mock the browser (sync_api -> launch_persistent_context) so the
adapter's captcha detection and row virtualisation are exercised without a real browser
or any network. The parser (rows_to_items / _normalize_author) is pure and browser-free.
"""
import sys
import os
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from litcli.backends.cnki import CnkiBackend, rows_to_items
from litcli.models import extract_year, normalize_author_name
from litcli import config


# --- pure parser / author normalisation ---

class CnkiParseTest(unittest.TestCase):
    def test_normalize_author_chinese_fullwidth(self):
        # Fullwidth comma and ideographic comma are both stripped; whole CJK name is family.
        self.assertEqual(normalize_author_name("熊英, 关晖"), {"family": "熊英关晖", "given": ""})
        self.assertEqual(normalize_author_name("熊英，吴锤结"), {"family": "熊英吴锤结", "given": ""})
        self.assertEqual(normalize_author_name("熊英、关晖"), {"family": "熊英关晖", "given": ""})

    def test_normalize_author_latin(self):
        self.assertEqual(normalize_author_name("John Smith"), {"family": "Smith", "given": "John"})
        self.assertEqual(normalize_author_name(""), {"family": "", "given": ""})

    def test_rows_to_items_skips_empty_title(self):
        rows = [
            {"title": "A real paper", "author": ["John Smith"], "container": "J", "year": "2020"},
            {"title": "", "author": [], "container": "", "year": ""},
            None,  # non-dict rows are tolerated
        ]
        items = rows_to_items(rows)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title, "A real paper")

    def test_rows_to_items_fields_and_source(self):
        rows = [{
            "title": "基于有限体积法的离散方法研究",
            "author": ["熊英, 关晖"],
            "container": "应用数学和力学",
            "year": "2016",
            "abstract": "",
            "url": "https://kns.cnki.net/kcms2/article/abstract?v=abc",
        }]
        items = rows_to_items(rows)
        it = items[0]
        self.assertEqual(it.source, "cnki")
        self.assertEqual(it.item_type, "article")
        self.assertEqual(it.title, "基于有限体积法的离散方法研究")
        self.assertEqual(it.creators[0]["family"], "熊英关晖")
        self.assertEqual(it.container_title, "应用数学和力学")
        self.assertEqual(it.year, "2016")
        self.assertEqual(it.url, "https://kns.cnki.net/kcms2/article/abstract?v=abc")
        self.assertIn("cnki", it.extra)

    def test_extract_year(self):
        self.assertEqual(extract_year("2026-10-10"), "2026")
        self.assertEqual(extract_year("2026-09-23 14:49"), "2026")
        self.assertEqual(extract_year("2016年6月"), "2016")
        self.assertEqual(extract_year("no digits"), "")
        self.assertEqual(extract_year(""), "")


# --- backend register ---

class CnkiRegisterTest(unittest.TestCase):
    def test_registry_contains_cnki(self):
        self.assertIn("cnki", config._registry())

    def test_discover_cnki_read_only(self):
        backends = config.discover_backends({"backends": {"cnki": {"type": "cnki"}}})
        self.assertEqual(backends["cnki"].name, "cnki")
        self.assertEqual(sorted(backends["cnki"].capabilities), ["read"])


# --- Playwright adapter (mocked browser, no network) ---

class _El:
    """Fake DOM element: inner_text + optional href."""

    def __init__(self, text, href=""):
        self._text = text
        self._href = href

    def inner_text(self):
        return self._text

    def get_attribute(self, n):
        return self._href if n == "href" else None


class _FakeNode:
    """Fake result row node modelled on the real CNKI `<tr>`: title `td.name a`, author
    `td.author a` (multiple), source `td.source`, date `td.date`."""

    def __init__(self, title, href, authors, source="", year="", seq="1"):
        # Simulate the cell structure so the adapter's `query_selector("td.name a")` etc.
        # resolve. Single-element selectors live in _cells; author selects return one _El
        # per author (matching the real DOM's multiple `td.author a`).
        self._cells = {
            "td.name a": _El(title, href),
            "td.name": _El(title),
            "a": _El(title, href),
            "td.source": _El(source),
            ".source": _El(source),
            "td.date": _El(year),
            ".date": _El(year),
        }
        self._author_els = [_El(a) for a in authors]
        self._text = f"{seq}\n{title}\n{';'.join(authors)}\n{source}\n{year}"

    def query_selector(self, sel):
        return self._cells.get(sel)

    def query_selector_all(self, sel):
        # The adapter asks for `td.author a, .author a`; answer with one element per author.
        if "author" in sel:
            return self._author_els
        return [self._cells.get(sel)] if self._cells.get(sel) else []

    def inner_text(self):
        return self._text


class _FakePage:
    """Fake Playwright page with configurable url / body text / result nodes."""

    def __init__(self, url="https://kns.cnki.net/kns8s/defaultresult/index", body="", nodes=None):
        self.url = url
        self._body = body
        self._nodes = nodes or []

    def goto(self, *a, **k):
        pass

    def wait_for_timeout(self, *a, **k):
        pass

    def evaluate(self, expr):
        return self._body

    def query_selector_all(self, sel):
        return self._nodes


class CnkiAdapterTest(unittest.TestCase):
    def test_detect_captcha_by_url(self):
        page = _FakePage(url="https://kns.cnki.net/verify/home?captchaType=blockPuzzle")
        be = CnkiBackend()
        self.assertTrue(be._detect_captcha(page))

    def test_detect_captcha_by_text(self):
        page = _FakePage(url="https://kns.cnki.net/kns8s/defaultresult/index",
                         body="请完成安全验证\n向右滑动完成验证")
        be = CnkiBackend()
        self.assertTrue(be._detect_captcha(page))

    def test_detect_captcha_false_on_normal_page(self):
        page = _FakePage(url="https://kns.cnki.net/kns8s/defaultresult/index",
                         body="基于有限体积法的离散方法研究\n熊英")
        be = CnkiBackend()
        self.assertFalse(be._detect_captcha(page))

    def test_virtualize_rows_extracts_title_author_source_year(self):
        be = CnkiBackend()
        node = _FakeNode("基于阴离子工程的钙钛矿电池研究", "/kcms2/article/abstract?v=x",
                         ["韦云霄", "张海兵"], source="化学学报", year="2026")
        page = _FakePage(nodes=[node])
        rows = be._virtualize_rows(page, limit=5)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertIn("基于阴离子工程", r["title"])
        self.assertIn("韦云霄", r["author"])
        self.assertIn("张海兵", r["author"])
        self.assertEqual(r["container"], "化学学报")
        self.assertEqual(r["year"], "2026")
        # _abs_url normalizes the relative href to an absolute CNKI URL.
        self.assertTrue(r["url"].startswith("https://kns.cnki.net/"))

    def test_missing_playwright_raises_clear_message(self):
        cnki = CnkiBackend()
        with mock.patch.dict(sys.modules, {"playwright.sync_api": None}):
            with self.assertRaises(RuntimeError) as ctx:
                cnki._playwright()
        self.assertIn("Playwright is not installed", str(ctx.exception))
        self.assertIn("--with-cnki", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
