"""Aggregate domain-layer unit tests: normalization / identity keys / dedup / merge / sort."""
import unittest
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from litcli.models import AcademicItem
from litcli.aggregate import norm_title, norm_doi, identity, merge_items


class NormTest(unittest.TestCase):
    def test_norm_title(self):
        self.assertEqual(norm_title("  L u- VED  "), "l u ved")
        self.assertEqual(norm_title("Hello,  World!"), "hello world")
        self.assertEqual(norm_title(""), "")

    def test_norm_doi(self):
        self.assertEqual(norm_doi("10.1/ABc."), "10.1/abc")
        self.assertEqual(norm_doi("  "), "")

    def test_norm_doi_url_prefix(self):
        # OpenAlex gives a full URL, Crossref a bare DOI; after stripping the prefix the identity keys match
        self.assertEqual(norm_doi("https://doi.org/10.1103/Physx.3.113301"),
                         "10.1103/physx.3.113301")
        self.assertEqual(norm_doi("http://dx.doi.org/10.1/x"), "10.1/x")

    def test_identity_merges_url_and_bare_doi(self):
        # Same work: crossref bare DOI vs openalex full URL -- should be judged the same identity
        a = AcademicItem(source="crossref", key="10.1103/x", title="T", doi="10.1103/x")
        b = AcademicItem(source="openalex", key="https://doi.org/10.1103/x",
                         title="T", doi="https://doi.org/10.1103/x")
        self.assertEqual(identity(a), identity(b))

    def test_identity_prefers_doi(self):
        a = AcademicItem(source="z", key="k", title="X", doi="10.1/a")
        b = AcademicItem(source="a", key="k2", title="Totally different title", doi="10.1/a")
        self.assertEqual(identity(a), identity(b))
        self.assertEqual(identity(a), "doi:10.1/a")

    def test_identity_title_fallback(self):
        a = AcademicItem(source="z", key="k", title="Hello  World!", doi="")
        b = AcademicItem(source="a", key="k2", title="hello world", doi="")
        self.assertEqual(identity(a), identity(b))


class MergeTest(unittest.TestCase):
    def test_dedup_and_merge_sources(self):
        a = AcademicItem(source="zotero", key="k1", title="LU-ved", doi="10.1/ABc.",
                         year="2002", abstract="abs", url="http://x")
        b = AcademicItem(source="arxiv", key="1701.00001", title="Lu-ved", doi="10.1/abc", year="2002")
        out = merge_items([a, b])
        self.assertEqual(len(out), 1)
        # Keep the primary record's original DOI snapshot, do not rewrite
        self.assertEqual(out[0].doi, "10.1/ABc.")
        # The more complete record (a) is primary; missing fields are backfilled
        self.assertEqual(out[0].abstract, "abs")
        self.assertEqual(out[0].url, "http://x")
        # Sources merged into an alphabetically sorted set
        self.assertEqual(out[0].extra["sources"], ["arxiv", "zotero"])

    def test_title_dedup_without_doi(self):
        c = AcademicItem(source="zotero", key="k2", title="Hello  World!", year="2020")
        d = AcademicItem(source="arxiv", key="9999.9999", title="hello world", year="2020")
        out = merge_items([c, d])
        self.assertEqual(len(out), 1)

    def test_year_desc_then_title_asc(self):
        c = AcademicItem(source="z", key="c", title="Alpha", year="2020")
        e = AcademicItem(source="x", key="e", title="Newer", year="2024")
        out = merge_items([c, e])
        self.assertEqual(out[0].year, "2024")
        # Same year ordered by title ascending
        f = AcademicItem(source="y", key="f", title="Zeta", year="2020")
        out2 = merge_items([c, f])
        self.assertEqual([x.title for x in out2], ["Alpha", "Zeta"])

    def test_does_not_mutate_input(self):
        a = AcademicItem(source="z", key="k1", title="T", doi="10.1/a")
        b = AcademicItem(source="a", key="k2", title="T", doi="10.1/a")
        items = [a, b]
        out = merge_items(items)
        self.assertEqual(len(items), 2)  # input not merged
        self.assertEqual(a.doi, "10.1/a")
        self.assertIsNot(out[0], a)

    def test_merge_backfills_container_fields(self):
        # crossref has volume/pages but no abstract; openalex has abstract but no volume/pages; after merging both are present
        a = AcademicItem(source="crossref", key="10.1/x", title="T", doi="10.1/x",
                         container_title="J. Fluid Mech.", volume="45", page="1-9")
        b = AcademicItem(source="openalex", key="10.1/x", title="T", doi="10.1/x",
                         abstract="abs")
        out = merge_items([a, b])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].container_title, "J. Fluid Mech.")
        self.assertEqual(out[0].page, "1-9")
        self.assertEqual(out[0].abstract, "abs")
        self.assertEqual(out[0].extra["sources"], ["crossref", "openalex"])

    def test_dedup_url_vs_bare_doi(self):
        # Real case: crossref bare DOI + openalex full-URL DOI, should merge to one record
        a = AcademicItem(source="crossref", key="10.1103/Phy.3.113301", title="Flow",
                         doi="10.1103/Phy.3.113301")
        b = AcademicItem(source="openalex", key="https://doi.org/10.1103/Phy.3.113301",
                         title="Flow", doi="https://doi.org/10.1103/Phy.3.113301")
        out = merge_items([a, b])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].extra["sources"], ["crossref", "openalex"])

    def test_merge_carries_cited_and_openalex_id(self):
        # Read-only backend (zotero has no citation count) vs openalex same year/title: the merge must
        # carry the citation count and openalex_id, otherwise --all --sort cited degrades to all 0 and
        # the `[cited N]` badge disappears.
        z = AcademicItem(source="zotero", key="AB12", title="Deep learning", year="2015",
                         abstract="abs", doi="10.1038/nature14539")
        o = AcademicItem(source="openalex", key="10.1038/nature14539", title="Deep learning",
                         year="2015", doi="10.1038/nature14539", cited_by_count=84467,
                         extra={"openalex_id": "https://openalex.org/W2919115771"})
        out = merge_items([z, o])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].cited_by_count, 84467)
        self.assertEqual(out[0].extra["openalex_id"], "https://openalex.org/W2919115771")


if __name__ == "__main__":
    unittest.main()
