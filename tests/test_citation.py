"""Citation rendering unit tests: in-text and bibliography, covering empty-author/year edges."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from litcli.models import AcademicItem
from litcli.citation import render_intext, render_bibliography


class TestCitation(unittest.TestCase):
    def test_intext_anon(self):
        it = AcademicItem(source="x", key="k", title="T", creators=[], year="2002")
        self.assertEqual(render_intext([it]), ["(Anon 2002)"])

    def test_intext_two(self):
        it = AcademicItem(source="x", key="k", title="T",
                          creators=[{"family": "A"}, {"family": "B"}], year="2002")
        self.assertEqual(render_intext([it]), ["(A & B 2002)"])

    def test_intext_many_et_al(self):
        it = AcademicItem(source="x", key="k", title="T", creators=[
            {"family": "A"}, {"family": "B"}, {"family": "C"}, {"family": "D"}], year="2002")
        self.assertEqual(render_intext([it]), ["(A et al. 2002)"])

    def test_bib_empty_creators_no_crash(self):
        # Original bug: AttributeError when it.author didn't exist while creators/year were both empty
        it = AcademicItem(source="x", key="k", title="Title here", creators=[], year="")
        out = render_bibliography([it])
        self.assertEqual(len(out), 1)
        self.assertIn("Title here", out[0])

    def test_bib_normal(self):
        it = AcademicItem(source="x", key="k", title="T",
                          creators=[{"family": "Smith", "given": "J"}],
                          year="2002", doi="10.1/abc")
        out = render_bibliography([it])
        self.assertIn("Smith, J", out[0])
        self.assertIn("(2002)", out[0])
        self.assertIn("doi:10.1/abc", out[0])


if __name__ == "__main__":
    unittest.main()
