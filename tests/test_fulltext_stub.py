"""fulltext legitimate-OA-channel stub tests: inject resolvers, assert priority / blocked marking /
single-step failure doesn't sink the run."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from litcli import fulltext as ft


def make_resolver(value):
    def r(doi):
        return value
    return r


class FulltextStubTest(unittest.TestCase):
    def test_priority_first_resolver_wins(self):
        cand = {"kind": "unpaywall", "url": "http://oa.example/p.pdf", "title": "T"}
        out = ft.resolve_fulltext("10.1/x", resolvers=[
            make_resolver(cand), make_resolver({"kind": "openalex", "url": "http://2.pdf"}),
        ])
        self.assertTrue(out["has_fulltext"])
        self.assertEqual(out["kind"], "unpaywall")
        self.assertEqual(out["url"], "http://oa.example/p.pdf")

    def test_second_resolver_used_when_first_none(self):
        out = ft.resolve_fulltext("10.1/x", resolvers=[
            make_resolver(None), make_resolver({"kind": "openalex", "url": "http://2.pdf"}),
        ])
        self.assertTrue(out["has_fulltext"])
        self.assertEqual(out["kind"], "openalex")

    def test_first_resolver_raises_skipped(self):
        def boom(d):
            raise RuntimeError("network down")
        out = ft.resolve_fulltext("10.1/x", resolvers=[
            boom, make_resolver({"kind": "openalex", "url": "http://2.pdf"}),
        ])
        self.assertTrue(out["has_fulltext"])
        self.assertEqual(out["kind"], "openalex")

    def test_no_fulltext_flagged(self):
        out = ft.resolve_fulltext("10.1/x", resolvers=[make_resolver(None)])
        self.assertFalse(out["has_fulltext"])

    def test_empty_url_not_accepted(self):
        out = ft.resolve_fulltext("10.1/x", resolvers=[
            make_resolver({"kind": "unpaywall", "url": ""}),
        ])
        self.assertFalse(out["has_fulltext"])

    def test_local_beats_oa(self):
        """A local library's attachment takes priority over an OA direct link (user requirement: local-first)."""
        class FakeBackend:
            name = "zotero"
            def can(self, cap):
                return cap == "attachment"

            def search(self, q, limit=10):
                return [type("I", (), {"key": "K1", "doi": q, "url": "", "title": "T"})]
            def get_attachment(self, key):
                return b"%PDF-1.4 local"
        out = ft.resolve_fulltext("10.1/x", backends={"zotero": FakeBackend()},
                                 resolvers=[
                                     lambda d: ft._from_local(d, {"zotero": FakeBackend()}),
                                     lambda d: {"kind": "openalex", "url": "http://oa.pdf"},
                                 ])
        self.assertTrue(out["has_fulltext"])
        self.assertEqual(out["kind"], "local")
        self.assertIn(b"%PDF", out["data"])

    def test_from_local_skips_backend_without_attachment(self):
        class NoAttach:
            name = "arxiv"
            def can(self, cap):
                return False  # no attachment capability, skip
        self.assertIsNone(ft._from_local("10.1/x", {"arxiv": NoAttach()}))


class CorpusResolverTest(unittest.TestCase):
    """Europe PMC / Zenodo resolvers: they only fire for items that actually carry a file."""

    class FakeBackend:
        def __init__(self, attachments):
            self.name = "fake"
            self.attachments = attachments

        def get(self, key):
            return type("I", (), {"title": "T", "attachments": self.attachments})

    class Missing:
        name = "missing"

        def get(self, key):
            raise KeyError("not found")

    def test_europepmc_xml_candidate_is_tagged(self):
        be = self.FakeBackend(["https://x.test/fullTextXML"])
        cand = ft._from_europepmc("10.1/x", be)
        self.assertEqual(cand["kind"], "europepmc")
        self.assertEqual(cand["format"], "xml")
        self.assertEqual(cand["url"], "https://x.test/fullTextXML")

    def test_zenodo_pdf_candidate(self):
        cand = ft._from_zenodo("10.1/x", self.FakeBackend(["https://x.test/a.pdf"]))
        self.assertEqual(cand["kind"], "zenodo")
        self.assertNotIn("format", cand)

    def test_no_attachment_and_missing_record_return_none(self):
        self.assertIsNone(ft._from_europepmc("10.1/x", self.FakeBackend([])))
        self.assertIsNone(ft._from_zenodo("10.1/x", self.FakeBackend([])))
        self.assertIsNone(ft._from_zenodo("10.1/x", self.Missing()))
        self.assertIsNone(ft._from_europepmc("10.1/x", None))

    @staticmethod
    def _target(resolver) -> str:
        """The ``_from_*`` function a resolver lambda wraps (it is a global, so in co_names)."""
        return next(n for n in resolver.__code__.co_names if n.startswith("_from_"))

    def test_resolver_order_keeps_scihub_last(self):
        # ScienceDirect sits after the free corpora but before the landing page: bytes beat a page.
        order = [self._target(r) for r in ft._default_resolvers("a@b.c")]
        self.assertEqual(order, ["_from_local", "_from_unpaywall", "_from_openalex",
                                 "_from_europepmc", "_from_zenodo", "_from_sciencedirect", "_from_wiley",
                                 "_from_unpaywall_landing", "_from_scihub"])

    def test_sciencedirect_candidate_is_entitled(self):
        cand = ft._from_sciencedirect("10.1/x", self.FakeBackend(["https://sd/x.pdf"]))
        self.assertEqual(cand["kind"], "sciencedirect")
        self.assertEqual(cand["access"], "entitled")
        self.assertNotIn("format", cand)

    def test_sciencedirect_without_a_pdf_falls_through(self):
        self.assertIsNone(ft._from_sciencedirect("10.1/x", self.FakeBackend([])))
        self.assertIsNone(ft._from_sciencedirect("10.1/x", self.Missing()))
        self.assertIsNone(ft._from_sciencedirect("10.1/x", None))

    def test_wiley_candidate_is_entitled(self):
        cand = ft._from_wiley("10.1002/x", self.FakeBackend(["https://w/x.pdf"]))
        self.assertEqual(cand["kind"], "wiley")
        self.assertEqual(cand["access"], "entitled")
        self.assertNotIn("format", cand)

    def test_wiley_without_a_pdf_falls_through(self):
        self.assertIsNone(ft._from_wiley("10.1002/x", self.FakeBackend([])))
        self.assertIsNone(ft._from_wiley("10.1002/x", self.Missing()))
        self.assertIsNone(ft._from_wiley("10.1002/x", None))

    def test_wiley_tdm_fallback_shows_the_page_not_the_api_url(self):
        class TdmOnly:
            def get(self, key):
                item = type("I", (), {
                    "title": "T", "url": "https://onlinelibrary.wiley.com/doi/10.1002/x",
                    "attachments": ["https://api.wiley.com/onlinelibrary/tdm/v1/articles/x"],
                    "extra": {"tdm_url": "https://api.wiley.com/onlinelibrary/tdm/v1/articles/x"},
                })
                return item

        cand = ft._from_wiley("10.1002/x", TdmOnly())
        self.assertEqual(cand["url"], "https://onlinelibrary.wiley.com/doi/10.1002/x")

    def test_tail_only_skips_everything_already_answered(self):
        self.assertEqual([self._target(r) for r in ft._default_resolvers("a@b.c", tail_only=True)],
                         ["_from_unpaywall_landing", "_from_scihub"])

    def test_entitled_pdf_beats_the_landing_page(self):
        with mock.patch.object(ft.unpaywall, "resolve_oa",
                               return_value={"best_pdf": "", "best_landing": "https://legal/page"}):
            out = ft.resolve_fulltext("10.1/x",
                                      sciencedirect_backend=self.FakeBackend(["https://sd/x.pdf"]))
        self.assertEqual(out["kind"], "sciencedirect")
        self.assertEqual(out["url"], "https://sd/x.pdf")

    def test_legal_landing_page_beats_scihub(self):
        class Sci:
            name = "scihub"

            def get(self, key):
                return type("I", (), {"title": "T", "attachments": ["https://sci/x.pdf"]})

        with mock.patch.object(ft.unpaywall, "resolve_oa",
                               return_value={"best_pdf": "", "best_landing": "https://legal/page"}):
            out = ft.resolve_fulltext("10.1/x", scihub_backend=Sci())
        self.assertEqual(out["kind"], "unpaywall")
        self.assertEqual(out["format"], "html")
        self.assertEqual(out["url"], "https://legal/page")

    def test_unpaywall_is_looked_up_once(self):
        with mock.patch.object(ft.unpaywall, "resolve_oa",
                               return_value={"best_pdf": "", "best_landing": "https://legal/page"}) as m:
            ft.resolve_fulltext("10.1/x")
        self.assertEqual(m.call_count, 1)

    def test_xml_to_text_flattens_and_collapses(self):
        xml = b"<?xml version='1.0'?><article><body><p>Hello  <b>world</b></p></body></article>"
        self.assertEqual(ft.xml_to_text(xml), "Hello world")

    def test_xml_to_text_keeps_block_lines(self):
        xml = b"<article><title>A</title><body><p>Hello</p><p>World</p></body></article>"
        self.assertEqual(ft.xml_to_text(xml), "A\nHello\nWorld")

    def test_html_to_text_strips_scripts_and_keeps_blocks(self):
        page = (b"<html><head><title>T</title><script>var x=1;</script></head>"
                b"<body><p>Hello <b>world</b></p><div>Next</div></body></html>")
        self.assertEqual(ft.html_to_text(page), "T\nHello world\nNext")

    def test_xml_to_text_on_garbage_is_empty(self):
        self.assertEqual(ft.xml_to_text(b"not xml at all"), "")


class NormDoiTest(unittest.TestCase):
    def test_strips_doi_org_prefix(self):
        # Must agree with aggregate.norm_doi so local-first resolution matches bare-DOI lookups.
        self.assertEqual(ft._norm_doi("https://doi.org/10.1/x"), "10.1/x")
        self.assertEqual(ft._norm_doi("10.1/X "), "10.1/x")
        self.assertEqual(ft._norm_doi("http://dx.doi.org/10.1/y."), "10.1/y")
        self.assertEqual(ft._norm_doi(""), "")


if __name__ == "__main__":
    unittest.main()
