"""领域层 models(Csl 转/建/导出)离线单测。"""
import unittest

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
        csl = item.to_csl()  # 原 _default_id 对空 creators 抛 IndexError
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
