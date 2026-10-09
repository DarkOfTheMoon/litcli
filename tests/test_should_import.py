"""收录准入门 should_import 桩测:查重(DOI/标题)+ 半残门。纯函数,不联网。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from litcli.models import AcademicItem
from litcli import fulltext as ft


def mk(doi="", title="T", creators=None, year="2020"):
    return AcademicItem(source="x", key="", title=title,
                        creators=creators if creators is not None else [{"family": "A", "given": "B"}],
                        year=year, doi=doi)


class ShouldImportStubTest(unittest.TestCase):
    def test_duplicate_doi_rejected(self):
        existing = [mk(doi="10.1/x", title="Other")]
        ok, reason = ft.should_import(mk(doi="10.1/x", title="A"), existing)
        self.assertFalse(ok)
        self.assertIn("DOI", reason)

    def test_duplicate_title_when_no_doi(self):
        existing = [mk(doi="", title="Big title")]
        ok, reason = ft.should_import(mk(doi="", title="BIG TITLE!"), existing)
        self.assertFalse(ok)
        self.assertIn("题", reason)

    def test_doi_differs_allows(self):
        existing = [mk(doi="10.1/a")]
        ok, _ = ft.should_import(mk(doi="10.1/b"), existing)
        self.assertTrue(ok)

    def test_missing_title_rejected(self):
        ok, reason = ft.should_import(mk(title=""), [])
        self.assertFalse(ok)
        self.assertIn("标题", reason)

    def test_no_creators_no_year_rejected(self):
        ok, reason = ft.should_import(mk(creators=[], year="", title="T"), [])
        self.assertFalse(ok)
        self.assertIn("作者", reason)

    def test_complete_allowed(self):
        ok, reason = ft.should_import(mk(), [])
        self.assertTrue(ok)
        self.assertEqual(reason, "")


if __name__ == "__main__":
    unittest.main()
