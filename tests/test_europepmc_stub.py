"""Europe PMC backend offline stub tests: query shape, author handling, OA XML hook."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from litcli.backends.europepmc import EuropePmcBackend, _creators, result_to_item
from tests.http_stub import speed_up, stub

LITE = {"id": "42471174", "source": "MED", "doi": "10.1016/j.ab.2026.116199",
        "title": "A novel finite volume solution of the Lamm equation.",
        "authorString": "Dobler L, Cao W, Martin R, Demeler B.",
        "pubYear": "2026", "journalTitle": "Analytical biochemistry", "isOpenAccess": "N"}

OA = {"id": "PMC13696468", "source": "PMC", "pmcid": "PMC13696468", "doi": "10.1/oa",
      "title": "Open article.", "authorList": {"author": [
          {"fullName": "Dobler L", "firstName": "Lukas", "lastName": "Dobler"}]},
      "pubYear": "2025", "isOpenAccess": "Y",
      "journalInfo": {"journal": {"title": "J Test"}, "volume": "3", "issue": "1"},
      "pageInfo": "e12", "abstractText": "<p>Abstract body</p>"}


def _payload(*results):
    return {"hitCount": len(results), "resultList": {"result": list(results)}}


class EuropePmcStubTest(unittest.TestCase):
    def setUp(self):
        self.be = EuropePmcBackend(delay=0)
        speed_up(self.be)

    def test_search_maps_fields_and_author_initials(self):
        rec = stub(self.be, (200, _payload(LITE)))
        items = self.be.search("lamm equation", limit=3, filters={"year": "2026"})
        url = rec.urls[0]
        self.assertIn("resultType=lite", url)
        self.assertIn("PUB_YEAR%3A2026", url)
        it = items[0]
        self.assertEqual(it.title, "A novel finite volume solution of the Lamm equation")
        # authorString is "Family Initials": the initials are the given name, not the family
        self.assertEqual(it.creators[0], {"family": "Dobler", "given": "L"})
        self.assertEqual(it.item_type, "article")
        self.assertEqual(it.attachments, [])  # not open access

    def test_oa_record_exposes_jats_xml(self):
        it = result_to_item(OA)
        self.assertEqual(it.attachments,
                         ["https://www.ebi.ac.uk/europepmc/webservices/rest/PMC13696468/fullTextXML"])
        self.assertEqual(it.creators, [{"family": "Dobler", "given": "Lukas"}])
        self.assertEqual(it.abstract, "Abstract body")
        self.assertEqual(it.container_title, "J Test")

    def test_search_clamps_page_size_to_the_api_maximum(self):
        rec = stub(self.be, (200, _payload()))
        self.be.search("lamm equation", limit=1001)
        self.assertIn("pageSize=1000", rec.urls[0])

    def test_get_by_doi_uses_core_result_type(self):
        rec = stub(self.be, (200, _payload(OA)))
        it = self.be.get("10.1/oa")
        url = rec.urls[0]
        self.assertIn("resultType=core", url)
        self.assertIn("DOI%3A%2210.1%2Foa%22", url)
        self.assertEqual(it.doi, "10.1/oa")

    def test_get_by_pmcid_uses_pmcid_field(self):
        rec = stub(self.be, (200, _payload(OA)))
        self.be.get("europepmc/PMC13696468")
        self.assertIn("PMCID%3APMC13696468", rec.urls[0])

    def test_suffix_and_group_authors_stay_separate(self):
        # "Smith JA Jr": the suffix belongs to Smith, not to the previous author.
        self.assertEqual(_creators({"authorString": "Doe J, Smith JA Jr"}),
                         [{"family": "Doe", "given": "J"}, {"family": "Smith Jr", "given": "JA"}])
        # A group author is its own creator, not a continuation of the previous family.
        self.assertEqual(_creators({"authorString": "Dobler L, The ABC Group"}),
                         [{"family": "Dobler", "given": "L"}, {"family": "The ABC Group", "given": ""}])
        # PubMed emits a bare suffix as its own comma chunk.
        self.assertEqual(_creators({"authorString": "Nopembri S, Tagare RL,  Jr, Meikahani R."}),
                         [{"family": "Nopembri", "given": "S"}, {"family": "Tagare Jr", "given": "RL"},
                          {"family": "Meikahani", "given": "R"}])

    def test_get_accepts_the_article_url_forms_it_prints(self):
        rec = stub(self.be, (200, _payload(OA)))
        self.be.get("https://europepmc.org/article/PMC/PMC13696468")
        self.assertIn("PMCID%3APMC13696468", rec.urls[0])
        rec = stub(self.be, (200, _payload(LITE)))
        self.be.get("https://europepmc.org/article/MED/42471174")
        self.assertIn("EXT_ID%3A42471174", rec.urls[0])

    def test_get_rejects_unrecognized_key(self):
        with self.assertRaises(KeyError):
            self.be.get("finite volume")

    def test_get_missing_raises(self):
        stub(self.be, (200, _payload()))
        with self.assertRaises(KeyError):
            self.be.get("10.1/absent")


SEED = {"resultList": {"result": [{"id": "30693666", "source": "MED", "doi": "10.1/seed",
                                      "title": "Seed", "pubYear": "2019"}]}}
REFERENCE = {"referenceList": {"reference": [
    {"id": "28963611", "source": "MED", "doi": "10.1/foundation", "title": "Sexual dimorphism.",
     "authorString": "Jaillon S.", "pubYear": "2018"},
]}}
CITATION = {"citationList": {"citation": [
    {"id": "41858289", "source": "MED", "doi": "10.1/citing", "title": "A citing paper.",
     "authorString": "Ke C, Hu W.", "journalAbbreviation": "J Leukoc Biol", "pubYear": "2026",
     "volume": "118", "issue": "4", "pageInfo": "qiag041", "citedByCount": 3},
]}}


class EdgeTest(unittest.TestCase):
    """The two directional axes: Europe PMC serves them for the records it indexes."""

    def setUp(self):
        self.be = EuropePmcBackend()
        speed_up(self.be)
        self.addCleanup(self.be.close)

    def test_both_directions_are_declared(self):
        self.assertTrue({"read", "refs", "citing"} <= self.be.capabilities)

    def test_citing_resolves_the_record_then_asks_the_citations_endpoint(self):
        rec = stub(self.be, (200, SEED), (200, CITATION))
        out = self.be.citing("10.1/seed", limit=5)
        self.assertTrue(any("/MED/30693666/citations" in u for u in rec.urls))
        self.assertEqual([(i.doi, i.title) for i in out],
                         [("10.1/citing", "A citing paper")])
        self.assertEqual(out[0].container_title, "J Leukoc Biol")

    def test_references_reads_its_own_endpoint_and_container(self):
        # EBI wraps the two directions differently: references live under ``referenceList``,
        # so reading ``citationList`` there answers empty forever.
        rec = stub(self.be, (200, SEED), (200, REFERENCE))
        out = self.be.references("10.1/seed")
        self.assertTrue(any("/MED/30693666/references" in u for u in rec.urls))
        self.assertEqual([(i.doi, i.title) for i in out],
                         [("10.1/foundation", "Sexual dimorphism")])

    def test_an_unresolvable_key_answers_empty(self):
        stub(self.be, (200, {"resultList": {"result": []}}))
        self.assertEqual(self.be.citing("10.1/nothing"), [])

    def test_the_limit_truncates(self):
        stub(self.be, (200, SEED), (200, CITATION))
        self.assertEqual(len(self.be.citing("10.1/seed", limit=1)), 1)

    def test_a_pmc_sourced_row_keys_as_a_pmcid(self):
        row = {"id": "7654321", "source": "PMC", "title": "An OA paper."}
        stub(self.be, (200, {"citationList": {"citation": [row]}}))
        out = self.be.citing("30693666")
        self.assertEqual(out[0].key, "PMC7654321")
        rec = stub(self.be, (200, {"citationList": {"citation": []}}))
        self.be.citing(out[0].key)
        self.assertTrue(any("/PMC/PMC7654321/citations" in u for u in rec.urls))

    def test_an_edge_row_keeps_an_id_get_can_take_back(self):
        # Rows without a DOI fall back to the identifier, and that key must be re-askable —
        # the renderer already puts the source name in front, so the key must not repeat it.
        row = {"id": "41858289", "source": "MED", "title": "A citing paper."}
        stub(self.be, (200, {"citationList": {"citation": [row]}}))
        out = self.be.citing("30693666")
        self.assertEqual(out[0].key, "41858289")
        rec = stub(self.be, (200, {"citationList": {"citation": []}}))
        self.be.citing(out[0].key)
        self.assertTrue(any("/MED/41858289/citations" in u for u in rec.urls))


if __name__ == "__main__":
    unittest.main()
