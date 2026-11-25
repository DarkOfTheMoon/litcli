"""OpenCitations (COCI): CC0 citation links, no key, no metadata.

It answers the two directional axes with bare DOIs and nothing else — the value is that it
keeps working when a metadata service is throttled or down. The walk hydrates the DOIs from a
metadata source; here the parse and the direction mapping are what is pinned.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from litcli.backends.opencitations import OpenCitationsBackend
from tests import http_stub

ROW = {"oci": "06-07", "citing": "10.1/citing", "cited": "10.1/seed", "creation": "2024-07"}


class Base(unittest.TestCase):
    def setUp(self):
        self.b = OpenCitationsBackend()
        http_stub.speed_up(self.b)
        self.addCleanup(self.b.close)


class CapabilityTest(Base):
    def test_both_directions_are_declared_and_related_is_not(self):
        self.assertTrue({"read", "refs", "citing"} <= self.b.capabilities)
        self.assertNotIn("related", self.b.capabilities)


class DirectionTest(Base):
    def test_citing_asks_the_citations_endpoint(self):
        stub = http_stub.stub(self.b, (200, [ROW]))
        out = self.b.citing("10.1/seed")
        self.assertIn("/api/v1/citations/10.1/seed", stub.urls[0])
        self.assertEqual([i.doi for i in out], ["10.1/citing"])

    def test_references_reads_the_cited_column(self):
        stub = http_stub.stub(self.b, (200, [ROW]))
        out = self.b.references("10.1/seed")
        self.assertIn("/api/v1/references/10.1/seed", stub.urls[0])
        self.assertEqual([i.doi for i in out], ["10.1/seed"])

    def test_the_limit_truncates(self):
        http_stub.stub(self.b, (200, [ROW, {**ROW, "citing": "10.1/other"}]))
        self.assertEqual(len(self.b.citing("10.1/seed", limit=1)), 1)


class KeyFormTest(Base):
    def test_the_doi_prefixes_are_stripped_before_the_path(self):
        for form in ("doi:10.1/seed", "DOI:10.1/seed", "https://doi.org/10.1/seed"):
            stub = http_stub.stub(self.b, (200, []))
            self.b.citing(form)
            self.assertIn("/api/v1/citations/10.1/seed", stub.urls[0])

    def test_get_strips_the_same_prefixes(self):
        self.assertEqual(self.b.get("https://doi.org/10.1/x").doi, "10.1/x")


class SearchTest(Base):
    def test_a_doi_shaped_query_answers_and_anything_else_does_not_bother(self):
        # COCI is a link index: a keyword search would only 404.
        self.assertEqual(self.b.search("sodium batteries"), [])
        self.assertEqual([i.doi for i in self.b.search("10.1/x")], ["10.1/x"])


class ToleranceTest(Base):
    def test_a_row_without_a_doi_is_skipped(self):
        http_stub.stub(self.b, (200, [{**ROW, "citing": ""}]))
        self.assertEqual(self.b.citing("10.1/seed"), [])

    def test_get_returns_the_bare_key(self):
        # No metadata service behind this backend: the item is the DOI, nothing more.
        item = self.b.get("10.1/x")
        self.assertEqual((item.key, item.doi, item.title), ("10.1/x", "10.1/x", ""))


if __name__ == "__main__":
    unittest.main()
