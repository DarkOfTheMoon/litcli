"""Sci-Hub backend offline stub tests: PoW solving, page parsing, mirror flow, chain priority.

The mirrors' HTTP is injected (``_FakeHttp``), so nothing here touches the network. The pure
helpers mirror the real page shapes captured by the 2026-10 probe: a ``/captcha/challenge/<id>``
ALTCHA page, then an article page carrying ``<meta name="citation_pdf_url">``.
"""
import base64
import hashlib
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from litcli import config, fulltext as ft
from litcli.backends import scihub

_PAGE = ('<html><head><meta name="citation_title" content="A Paper">'
         '<meta name="citation_author" content="Smith, John">'
         '<meta name="citation_journal_title" content="J. Test">'
         '<meta name="citation_publication_date" content="2016/02/11">'
         '<meta name="citation_doi" content="10.1103/X">'
         '<meta name="citation_pdf_url" content="/storage/zero/1/a.pdf"></head></html>')
_CAPTCHA = ('<html><body><altcha-widget challengeurl="/captcha/challenge/777"></altcha-widget>'
            "<script>fetch('/captcha/solution/777')</script></body></html>")


def _challenge(number=42, algorithm="SHA-256", salt="abc123456789?expires=1&"):
    return {"algorithm": algorithm, "challenge":
            hashlib.new(algorithm.replace("-", "").lower(), (salt + str(number)).encode()).hexdigest(),
            "maxNumber": 200000, "salt": salt, "signature": "sig"}


class ScihubParseTest(unittest.TestCase):
    def test_solve_altcha_sha256(self):
        payload = scihub.solve_altcha(_challenge())
        data = json.loads(base64.b64decode(payload))
        self.assertEqual(data["number"], 42)
        self.assertEqual(data["signature"], "sig")
        # The salt is hashed verbatim (its ?expires suffix stays in the string).
        self.assertEqual(data["salt"], "abc123456789?expires=1&")

    def test_solve_altcha_sha384(self):
        self.assertIn("number", json.loads(base64.b64decode(
            scihub.solve_altcha(_challenge(algorithm="SHA-384")))))

    def test_solve_altcha_malformed(self):
        with self.assertRaises(RuntimeError):
            scihub.solve_altcha({"algorithm": "SHA-256"})
        with self.assertRaises(RuntimeError):
            scihub.solve_altcha({"algorithm": "MD5", "salt": "s", "challenge": "c"})

    def test_captcha_id_from_page(self):
        self.assertEqual(scihub.captcha_id(_CAPTCHA), "777")
        self.assertEqual(scihub.captcha_id(_PAGE), "")

    def test_extract_pdf_url_meta(self):
        self.assertEqual(scihub.extract_pdf_url(_PAGE, "https://m.test/10.1/x"),
                         "https://m.test/storage/zero/1/a.pdf")

    def test_extract_pdf_url_attribute_order_and_embed(self):
        reversed_tag = '<meta content="/z.pdf" name="citation_pdf_url">'
        self.assertEqual(scihub.extract_pdf_url(reversed_tag, "https://m.test/x"),
                         "https://m.test/z.pdf")
        self.assertEqual(scihub.extract_pdf_url('<embed src="/e.pdf">', "https://m.test/x"),
                         "https://m.test/e.pdf")
        self.assertEqual(scihub.extract_pdf_url('<iframe src="//cdn.test/i.pdf">',
                                                "https://m.test/x"),
                         "https://cdn.test/i.pdf")

    def test_extract_pdf_url_absent(self):
        self.assertEqual(scihub.extract_pdf_url("<html>не найдено</html>", "https://m.test/x"), "")

    def test_parse_citation_meta(self):
        meta = scihub.parse_citation_meta(_PAGE)
        self.assertEqual(meta["title"], "A Paper")
        self.assertEqual(meta["authors"], ["Smith, John"])
        self.assertEqual(meta["container"], "J. Test")
        self.assertEqual(meta["doi"], "10.1103/X")

    def test_doi_from_forms(self):
        self.assertEqual(scihub.doi_from("10.1103/X"), "10.1103/X")
        self.assertEqual(scihub.doi_from("scihub/10.1103/X"), "10.1103/X")
        self.assertEqual(scihub.doi_from("https://doi.org/10.1103/X"), "10.1103/X")
        self.assertEqual(scihub.doi_from("doi: 10.1103/X"), "10.1103/X")
        self.assertEqual(scihub.doi_from("finite volume method"), "")


class _FakeHttp:
    """Minimal stand-in for ``scihub._Http``: serves per-URL pages, records POSTs."""

    def __init__(self, pages=None, fail=()):
        self.pages = pages or {}
        self.fail = set(fail)
        self.posts = []

    def _get(self, url):
        if url in self.fail:
            raise RuntimeError("boom")
        body = self.pages.get(url, "")
        if isinstance(body, list):  # successive responses for one URL (captcha -> unlocked page)
            body = body.pop(0) if body else ""
        return url, body

    def get_text(self, url):
        return self._get(url)

    def get_json(self, url):
        return self._get(url)[1]

    def post_json(self, url, payload):
        self.posts.append((url, payload))
        return {"success": True}


class ScihubResolveTest(unittest.TestCase):
    def _http(self):
        return _FakeHttp(pages={
            "https://sci-hub.ru/10.1103/X": [_CAPTCHA, _PAGE],
            "https://sci-hub.ru/captcha/challenge/777": _challenge(),
            "https://sci-hub.ru/captcha/solution/777": "{}",
        })

    def test_resolve_solves_captcha_and_returns_pdf(self):
        http = self._http()
        out = scihub.resolve("10.1103/X", mirrors=["sci-hub.ru"], http=http)
        self.assertEqual(out["pdf_url"], "https://sci-hub.ru/storage/zero/1/a.pdf")
        self.assertEqual(len(http.posts), 1)
        self.assertEqual(http.posts[0][0], "https://sci-hub.ru/captcha/solution/777")

    def test_resolve_falls_through_broken_mirror(self):
        http = _FakeHttp(pages={"https://sci-hub.ru/10.1103/X": _PAGE})
        out = scihub.resolve("10.1103/X", mirrors=["sci-hub.st", "sci-hub.ru"],
                             http=http)
        self.assertEqual(out["mirror"], "sci-hub.ru")

    def test_resolve_not_found_raises_keyerror(self):
        http = _FakeHttp(pages={"https://sci-hub.ru/10.1103/X": "<html>не найдено</html>"})
        with self.assertRaises(KeyError):
            scihub.resolve("10.1103/X", mirrors=["sci-hub.ru"], http=http)

    def test_resolve_all_mirrors_fail_raises_runtime(self):
        http = _FakeHttp(fail={"https://sci-hub.ru/10.1103/X", "https://sci-hub.st/10.1103/X"})
        with self.assertRaises(RuntimeError):
            scihub.resolve("10.1103/X", mirrors=["sci-hub.ru", "sci-hub.st"], http=http)

    def test_resolve_rejects_non_doi(self):
        with self.assertRaises(KeyError):
            scihub.resolve("finite volume", http=_FakeHttp())


class ScihubBackendTest(unittest.TestCase):
    def test_read_only_capabilities(self):
        self.assertEqual(sorted(scihub.ScihubBackend().capabilities), ["read"])

    def test_search_doi_query_returns_item(self):
        be = scihub.ScihubBackend()
        with mock.patch.object(scihub, "resolve", return_value={
                "doi": "10.1103/X", "mirror": "m", "page_url": "https://m/p",
                "pdf_url": "https://m/a.pdf",
                "meta": scihub.parse_citation_meta(
                    _PAGE + '<meta name="citation_author" content="et., al.">')}):
            items = be.search("10.1103/X")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].doi, "10.1103/X")
        self.assertEqual(items[0].attachments, ["https://m/a.pdf"])
        self.assertEqual(items[0].title, "A Paper")
        self.assertEqual(items[0].creators, [{"family": "Smith", "given": "John"}])

    def test_search_keyword_returns_empty_without_network(self):
        be = scihub.ScihubBackend()
        with mock.patch.object(scihub, "resolve") as res:
            self.assertEqual(be.search("finite volume method"), [])
        res.assert_not_called()

    def test_search_not_found_is_empty(self):
        be = scihub.ScihubBackend()
        with mock.patch.object(scihub, "resolve", side_effect=KeyError("nope")):
            self.assertEqual(be.search("10.1103/X"), [])

    def test_get_attachment_downloads(self):
        be = scihub.ScihubBackend()
        with mock.patch.object(scihub, "resolve", return_value={
                "doi": "10.1103/X", "mirror": "m", "page_url": "https://m/p",
                "pdf_url": "https://m/a.pdf", "meta": scihub.parse_citation_meta(_PAGE)}), \
             mock.patch.object(scihub.pdfutil, "download", return_value=b"%PDF-1.4") as dl:
            self.assertEqual(be.get_attachment("10.1103/X"), b"%PDF-1.4")
        dl.assert_called_once_with("https://m/a.pdf", timeout=30)

    def test_discover_scihub_read_only(self):
        backends = config.discover_backends({"backends": {"scihub": {"type": "scihub"}}})
        self.assertEqual(sorted(backends["scihub"].capabilities), ["read"])


class ScihubChainTest(unittest.TestCase):
    def test_scihub_runs_last(self):
        class Sci:
            def get(self, doi):
                return type("I", (), {"attachments": ["https://sci/a.pdf"], "title": "S"})()

        out = ft.resolve_fulltext("10.1/x", scihub_backend=Sci(), resolvers=[
            lambda d: None, lambda d: ft._from_scihub(d, Sci())])
        self.assertEqual(out["kind"], "scihub")
        self.assertEqual(out["url"], "https://sci/a.pdf")

    def test_no_scihub_backend_is_noop(self):
        self.assertIsNone(ft._from_scihub("10.1/x", None))

    def test_from_scihub_swallows_lookup_errors(self):
        class Broken:
            def get(self, doi):
                raise RuntimeError("down")
        self.assertIsNone(ft._from_scihub("10.1/x", Broken()))

    def test_oa_still_beats_scihub(self):
        out = ft.resolve_fulltext("10.1/x", scihub_backend=object(), resolvers=[
            lambda d: {"kind": "unpaywall", "url": "https://oa/p.pdf"}])
        self.assertEqual(out["kind"], "unpaywall")


if __name__ == "__main__":
    unittest.main()
