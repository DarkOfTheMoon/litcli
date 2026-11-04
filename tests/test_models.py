"""Domain-layer models (CSL convert/build/export) offline unit tests."""
import unittest
from typing import ClassVar

from litcli.models import AcademicItem


class TestModels(unittest.TestCase):
    def test_to_csl(self):
        it = AcademicItem(source="zotero", key="K1", title="T",
                          creators=[{"family": "Smith", "given": "J"}],
                          year="2020", doi="10.x", item_type="article")
        c = it.to_csl()
        self.assertEqual(c["type"], "article")
        self.assertEqual(c["author"][0]["family"], "Smith")
        self.assertEqual(c["issued"]["date-parts"], [[2020]])

    def test_from_csl_roundtrip(self):
        it = AcademicItem.from_csl(
            {"type": "article", "title": "X",
             "author": [{"family": "A", "given": "B"}],
             "issued": {"date-parts": [[2019]]}, "DOI": "10.y"},
            source="zotero")
        self.assertEqual(it.year, "2019")
        self.assertEqual(it.creators[0]["family"], "A")
        self.assertEqual(it.doi, "10.y")

    def test_bibtex(self):
        it = AcademicItem(title="X", creators=[{"family": "A", "given": "B"}], year="2020")
        self.assertIn("@", it.to_bibtex())
        self.assertIn("author", it.to_bibtex())

    def test_three_plus_authors_intext(self):
        it = AcademicItem(title="X", year=2020, creators=[
            {"family": "A"}, {"family": "B"}, {"family": "C"}, {"family": "D"}])
        self.assertEqual(len(it.creators), 4)


if __name__ == "__main__":
    unittest.main()


class TestAcademicItemToCslEmpty(unittest.TestCase):
    def test_empty_creators_does_not_crash(self):
        from litcli.models import AcademicItem
        item = AcademicItem(source="pdf", key="", title="T", creators=[], year="")
        csl = item.to_csl()  # original _default_id threw IndexError on empty creators
        self.assertIn("id", csl)
        self.assertEqual(csl["id"], "anon_noyear")


class TestAcademicItemToRis(unittest.TestCase):
    def test_ris_default(self):
        from litcli.models import AcademicItem
        item = AcademicItem(source="z", key="k", title="T", item_type="article",
                            creators=[{"family": "Smith", "given": "John"}],
                            year="2002", doi="10.1/x", url="http://x")
        ris = item.to_ris()
        self.assertTrue(ris.startswith("TY  - JOUR"))
        self.assertIn("AU  - Smith, John", ris)
        self.assertIn("PY  - 2002", ris)
        self.assertIn("TI  - T", ris)
        self.assertIn("DO  - 10.1/x", ris)
        self.assertTrue(ris.rstrip().endswith("ER  -"))

    def test_ris_book_type(self):
        from litcli.models import AcademicItem
        it = AcademicItem(source="z", key="k", title="B", item_type="book", year="1999")
        self.assertTrue(it.to_ris().startswith("TY  - BOOK"))


class TestExportContainerFields(unittest.TestCase):
    """Backfilled export fields: container-title/volume/issue/page/publisher should enter csl/bibtex/ris."""

    ARTL: ClassVar[dict] = dict(source="crossref", key="10.1/abc", title="T",
                creators=[{"family": "Smith", "given": "J"}], year="2020",
                container_title="J. Fluid Mech.", volume="45", issue="3", page="123-130",
                doi="10.1/abc", url="https://doi.org/10.1/abc", item_type="article")

    def test_to_csl_includes_container_fields(self):
        c = AcademicItem(**self.ARTL).to_csl()
        self.assertEqual(c["container-title"], "J. Fluid Mech.")
        self.assertEqual(c["volume"], "45")
        self.assertEqual(c["issue"], "3")
        self.assertEqual(c["page"], "123-130")

    def test_to_bibtex_includes_journal_vol_pages(self):
        b = AcademicItem(**self.ARTL).to_bibtex()
        self.assertIn("journal = {J. Fluid Mech.}", b)
        self.assertIn("volume  = {45}", b)
        self.assertIn("number  = {3}", b)
        self.assertIn("pages   = {123-130}", b)
        self.assertTrue(b.startswith("@article{"))
        self.assertIn("\n  ", b)  # multi-line output

    def test_to_bibtex_book_uses_booktitle(self):
        it = AcademicItem(source="crossref", key="10.1/b", title="Chapter",
                          container_title="The Big Book", item_type="chapter")
        self.assertIn("booktitle = {The Big Book}", it.to_bibtex())

    def test_to_bibtex_article_uses_journal_not_booktitle(self):
        it = AcademicItem(source="crossref", key="10.1/a", title="A",
                          container_title="J. Phys.", item_type="article")
        self.assertIn("journal = {J. Phys.}", it.to_bibtex())
        self.assertNotIn("booktitle", it.to_bibtex())

    def test_to_ris_includes_journal_vol_issue_pages(self):
        ris = AcademicItem(**self.ARTL).to_ris()
        self.assertIn("JF  - J. Fluid Mech.", ris)
        self.assertIn("VL  - 45", ris)
        self.assertIn("IS  - 3", ris)
        self.assertIn("SP  - 123", ris)
        self.assertIn("EP  - 130", ris)
        self.assertIn("DO  - 10.1/abc", ris)

    def test_ris_single_page_only(self):
        it = AcademicItem(source="z", key="k", title="T", item_type="article", page="7")
        ris = it.to_ris()
        self.assertIn("SP  - 7", ris)
        self.assertNotIn("EP  -", ris)

    def test_from_csl_reads_container_fields(self):
        it = AcademicItem.from_csl({"type": "article", "title": "X",
                                    "container-title": "Nature", "volume": "1",
                                    "issue": "2", "page": "3-4", "publisher": "ACME"})
        self.assertEqual(it.container_title, "Nature")
        self.assertEqual(it.volume, "1")
        self.assertEqual(it.issue, "2")
        self.assertEqual(it.page, "3-4")
        self.assertEqual(it.publisher, "ACME")

    def test_render_bibliography_includes_container(self):
        from litcli.citation import render_bibliography
        it = AcademicItem(**self.ARTL)
        entry = render_bibliography([it])[0]
        self.assertIn("J. Fluid Mech.", entry)
        self.assertIn("45", entry)
        self.assertIn("123-130", entry)


class TestExportBookFields(unittest.TestCase):
    """Book isbn/edition should enter csl/bibtex/ris and round-trip via from_csl."""

    BOOK: ClassVar[dict] = dict(source="douban", key="subject/3061999", title="有限体积法基础",
                creators=[{"family": "李仁宪"}], year="1998-09",
                publisher="国防工业出版社", isbn="9787118048348", edition="第1版",
                item_type="book", url="https://book.douban.com/subject/3061999/")

    def test_to_csl_includes_isbn_edition(self):
        c = AcademicItem(**self.BOOK).to_csl()
        self.assertEqual(c["ISBN"], "9787118048348")
        self.assertEqual(c["edition"], "第1版")

    def test_to_bibtex_includes_isbn_edition(self):
        b = AcademicItem(**self.BOOK).to_bibtex()
        self.assertIn("isbn    = {9787118048348}", b)
        self.assertIn("edition = {第1版}", b)

    def test_to_ris_includes_isbn_edition(self):
        r = AcademicItem(**self.BOOK).to_ris()
        self.assertIn("SN  - 9787118048348", r)
        self.assertIn("ET  - 第1版", r)

    def test_from_csl_reads_isbn_edition(self):
        it = AcademicItem.from_csl({"type": "book", "title": "T",
                                    "ISBN": "9787118048348", "edition": "第2版"})
        self.assertEqual(it.isbn, "9787118048348")
        self.assertEqual(it.edition, "第2版")


class TestExportHardening(unittest.TestCase):
    """Export boundary: a non-numeric year or a multiline/literal-author field must not crash or corrupt output."""

    def test_to_csl_non_numeric_year_does_not_crash(self):
        it = AcademicItem(title="T", year="2020 (in press)", item_type="article")
        c = it.to_csl()
        self.assertEqual(c["issued"]["date-parts"], [[2020]])

    def test_to_csl_garbled_year_no_issued_key(self):
        it = AcademicItem(title="T", year="in press", item_type="article")
        c = it.to_csl()
        self.assertNotIn("issued", c)

    def test_ris_multiline_abstract_flattened(self):
        it = AcademicItem(title="T", item_type="article", abstract="line1\nline2\nline3")
        ris = it.to_ris()
        self.assertIn("AB  - line1 line2 line3", ris)
        self.assertEqual(ris.count("\n"), ris.count("  - ") + ris.count("ER  -"))

    def test_bibtex_multiline_title_flattened(self):
        it = AcademicItem(title="A title\nwith a break", creators=[{"family": "A"}])
        self.assertIn("title   = {A title with a break}", it.to_bibtex())

    def test_literal_author_renders_in_ris_and_bibtex(self):
        it = AcademicItem(title="T", creators=[{"literal": "MIT"}], year="2020")
        self.assertIn("AU  - MIT", it.to_ris())
        self.assertIn("author  = {MIT}", it.to_bibtex())

    def test_csl_author_with_literal(self):
        it = AcademicItem(title="T", creators=[{"literal": "MIT"}])
        self.assertEqual(it.to_csl()["author"], [{"literal": "MIT"}])

    def test_csl_author_strips_unknown_keys(self):
        it = AcademicItem(title="T", creators=[{"family": "A", "given": "B", "ORCID": "x"}])
        self.assertEqual(it.to_csl()["author"], [{"family": "A", "given": "B"}])
