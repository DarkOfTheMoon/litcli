"""Semantic Scholar: the graph source that answers all three axes without a key.

It is the closest stand-in for OpenAlex, so it is also the first fallback when OpenAlex's
shared free budget is spent: same three directions (references, citations, recommendations
for the semantic neighbours), same shape of metadata. No key here — the key only lifts the
rate.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from litcli.backends.semanticscholar import SemanticScholarBackend
from litcli.errors import NotFound, Unavailable
from tests import http_stub

PAPER = {
    "paperId": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "title": "A citing work",
    "year": 2021,
    "abstract": "About blades.",
    "venue": "Energies",
    "citationCount": 12,
    "externalIds": {"DOI": "10.1/citing", "CorpusId": 9},
    "openAccessPdf": {"url": "https://oa/x.pdf"},
    "authors": [{"name": "Ada Lovelace"}],
}


class Base(unittest.TestCase):
    def setUp(self):
        self.b = SemanticScholarBackend(mailto="me@example.org")
        http_stub.speed_up(self.b)
        self.addCleanup(self.b.close)


class CapabilityTest(Base):
    def test_all_three_axes_are_declared(self):
        self.assertTrue({"read", "refs", "citing", "related"} <= self.b.capabilities)


class ReferencesTest(Base):
    def test_references_parse_into_items(self):
        stub = http_stub.stub(self.b, (200, {"data": [{"citedPaper": PAPER}]}))
        out = self.b.references("DOI:10.1/seed", limit=3)
        self.assertEqual(len(out), 1)
        item = out[0]
        self.assertEqual((item.title, item.year, item.doi, item.container_title),
                         ("A citing work", "2021", "10.1/citing", "Energies"))
        self.assertEqual(item.attachments, ["https://oa/x.pdf"])
        self.assertEqual(item.creators, [{"family": "Lovelace", "given": "Ada"}])
        self.assertIn("/paper/DOI:10.1/seed/references", stub.urls[0])
        self.assertIn("limit=3", stub.urls[0])

    def test_a_null_paper_slot_is_skipped(self):
        http_stub.stub(self.b, (200, {"data": [{"citedPaper": None}]}))
        self.assertEqual(self.b.references("DOI:10.1/x"), [])


class CitingTest(Base):
    def test_citations_parse_the_other_way(self):
        stub = http_stub.stub(self.b, (200, {"data": [{"citingPaper": PAPER}]}))
        out = self.b.citing("DOI:10.1/seed", limit=2)
        self.assertEqual([i.doi for i in out], ["10.1/citing"])
        self.assertIn("/paper/DOI:10.1/seed/citations", stub.urls[0])


class RelatedTest(Base):
    def test_recommendations_are_the_neighbour_axis(self):
        # S2 answers recommendations for a paper: the semantic neighbours OpenAlex keeps inline.
        stub = http_stub.stub(self.b, (200, {"recommendedPapers": [PAPER]}))
        out = self.b.related("DOI:10.1/seed", limit=4)
        self.assertEqual([i.doi for i in out], ["10.1/citing"])
        self.assertIn("/recommendations/v1/papers/forpaper/DOI:10.1/seed", stub.urls[0])

    def test_the_seed_itself_is_dropped_from_the_recommendations(self):
        http_stub.stub(self.b, (200, {"recommendedPapers": [PAPER]}))
        self.assertEqual(self.b.related("DOI:10.1/citing"), [])

    def test_dropping_the_seed_does_not_cut_the_page_short(self):
        # One extra row is asked for, so a seed in the lead still yields a full ``limit``.
        other = {**PAPER, "paperId": "d" * 40, "externalIds": {"DOI": "10.1/other"}}
        stub = http_stub.stub(self.b, (200, {"recommendedPapers": [PAPER, other]}))
        self.assertEqual([i.doi for i in self.b.related("DOI:10.1/citing", limit=1)],
                         ["10.1/other"])
        self.assertIn("limit=2", stub.urls[0])


class GetTest(Base):
    def test_a_bare_doi_becomes_the_documented_identifier(self):
        stub = http_stub.stub(self.b, (200, PAPER))
        item = self.b.get("10.1/citing")
        self.assertEqual(item.doi, "10.1/citing")
        self.assertIn("/paper/DOI:10.1/citing", stub.urls[0])

    def test_a_paper_id_and_a_url_are_accepted(self):
        pid = "a" * 40
        stub = http_stub.stub(self.b, (200, PAPER))
        self.b.get(f"https://www.semanticscholar.org/paper/{pid}")
        self.assertIn(f"/paper/{pid}", stub.urls[0])
        stub = http_stub.stub(self.b, (200, PAPER))
        self.b.get(pid)
        self.assertIn(f"/paper/{pid}", stub.urls[0])

    def test_a_missing_paper_is_not_found(self):
        http_stub.stub(self.b, (404, {"error": "not found"}))
        with self.assertRaises(NotFound):
            self.b.get("10.1/nope")


class OrderingTest(Base):
    def test_the_edges_come_back_citation_descending(self):
        # S2 has no sort parameter on the edges; the bibliography contract is citation-desc.
        low = {**PAPER, "paperId": "b" * 40, "externalIds": {"DOI": "10.1/low"}, "citationCount": 2}
        high = {**PAPER, "paperId": "c" * 40, "externalIds": {"DOI": "10.1/high"}, "citationCount": 90}
        http_stub.stub(self.b, (200, {"data": [{"citedPaper": low}, {"citedPaper": high}]}))
        self.assertEqual([i.doi for i in self.b.references("DOI:10.1/seed", limit=5)],
                         ["10.1/high", "10.1/low"])

    def test_the_limit_is_applied_after_the_sort(self):
        low = {**PAPER, "externalIds": {"DOI": "10.1/low"}, "citationCount": 2}
        high = {**PAPER, "externalIds": {"DOI": "10.1/high"}, "citationCount": 90}
        http_stub.stub(self.b, (200, {"data": [{"citedPaper": low}, {"citedPaper": high}]}))
        self.assertEqual([i.doi for i in self.b.references("DOI:10.1/seed", limit=1)],
                         ["10.1/high"])


class IdentityTest(Base):
    def test_a_paper_without_a_doi_keeps_its_paper_id(self):
        # The walk must still be able to expand it; a bare title would not be askable.
        bare = {**PAPER, "externalIds": {"CorpusId": 9}}
        http_stub.stub(self.b, (200, {"data": [{"citedPaper": bare}]}))
        out = self.b.references("DOI:10.1/seed")
        self.assertEqual(out[0].key, "a" * 40)
        self.assertEqual(out[0].doi, "")

    def test_a_non_doi_key_is_not_given_a_doi_prefix(self):
        stub = http_stub.stub(self.b, (200, PAPER))
        self.b.get("W2919115771")
        self.assertIn("/paper/W2919115771", stub.urls[0])
        self.assertNotIn("DOI%3A", stub.urls[0])


class KeyTest(Base):
    def test_a_configured_key_travels_as_the_header(self):
        b = SemanticScholarBackend(mailto="me@example.org", api_key="k-123")
        http_stub.speed_up(b)
        self.addCleanup(b.close)
        self.assertEqual(b.headers.get("x-api-key"), "k-123")

    def test_no_key_means_no_header(self):
        self.assertNotIn("x-api-key", self.b.headers)


class FailureTest(Base):
    def test_a_spent_budget_surfaces_as_unavailable(self):
        # S2 throttles the shared unauthenticated pool hard; the axis caller must see it.
        http_stub.stub(self.b, (429, {"error": "Too many requests"}))
        self.b.retries = 1
        with self.assertRaises(Unavailable):
            self.b.references("DOI:10.1/x")


if __name__ == "__main__":
    unittest.main()
