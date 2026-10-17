"""Offline unit tests for the metadata.py heuristic-extraction pure functions (no pdftotext, no network)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from litcli import metadata as md


class FromTextLinesTest(unittest.TestCase):
    def test_builds_item_from_clean_first_page(self):
        lines = ["A Deep Learning Approach to Finite Volume Methods",
                 "John A. Smith, Mary B. Jones",
                 "2018"]
        item = md.from_text_lines(lines, source="/tmp/p.pdf")
        self.assertEqual(item.source, "pdf")
        self.assertEqual(item.item_type, "article")
        self.assertEqual(item.title, "A Deep Learning Approach to Finite Volume Methods")
        self.assertEqual(item.year, "2018")
        self.assertEqual(item.creators, [
            {"family": "Smith", "given": "John A."},
            {"family": "Jones", "given": "Mary B."},
        ])
        self.assertEqual(item.extra["source"], "/tmp/p.pdf")

    def test_missing_fields_fall_back_empty(self):
        item = md.from_text_lines([])
        self.assertEqual(item.title, "")
        self.assertEqual(item.creators, [])
        self.assertEqual(item.year, "")
        self.assertEqual(item.abstract, "")


class GuessYearTest(unittest.TestCase):
    def test_extracts_four_digit_year(self):
        self.assertEqual(md._guess_year(["No year here", "Published 2019"]), "2019")
        self.assertEqual(md._guess_year(["20", "1999"]), "1999")

    def test_no_year_returns_empty(self):
        self.assertEqual(md._guess_year(["No year"]), "")
        self.assertEqual(md._guess_year(["19", "9"]), "")


class GuessAbstractTest(unittest.TestCase):
    def test_collects_text_after_abstract_header(self):
        lines = ["Title", "Abstract", "We study the method.", "More text."]
        self.assertIn("We study the method.", md._guess_abstract(lines))
        self.assertIn("More text.", md._guess_abstract(lines))

    def test_no_abstract_returns_empty(self):
        self.assertEqual(md._guess_abstract(["Title", "No abstract here"]), "")


class SplitAuthorsTest(unittest.TestCase):
    def test_splits_by_comma_and_and(self):
        authors = md._split_authors(["A B Smith, C D Jones, and E F Lee"])
        self.assertEqual(authors, [
            {"family": "Smith", "given": "A B"},
            {"family": "Jones", "given": "C D"},
            {"family": "Lee", "given": "E F"},
        ])

    def test_single_name(self):
        self.assertEqual(md._split_authors(["Einstein"]), [{"family": "Einstein", "given": ""}])


class LooksLikeAuthorLineTest(unittest.TestCase):
    def test_requires_separator_and_capital_names(self):
        self.assertFalse(md._looks_like_author_line("John A. Smith"))  # no , ; and &
        self.assertTrue(md._looks_like_author_line("John A. Smith, Mary B. Jones"))
        self.assertFalse(md._looks_like_author_line("a short line"))   # no capital pairing
        self.assertFalse(md._looks_like_author_line("2020"))           # year


class CleanTitleTest(unittest.TestCase):
    def test_strips_arxiv_prefix_and_punctuation(self):
        self.assertEqual(md._clean_title("arXiv:2001.00001 Some Title"), "Some Title")
        self.assertEqual(md._clean_title("  My Great Paper.  "), "My Great Paper")


class GuessAuthorsTest(unittest.TestCase):
    def test_stops_at_year_or_abstract(self):
        lines = ["Some Title", "A B Smith, C D Jones", "2020", "E F Lee, G H Wu"]
        self.assertEqual(md._guess_authors(lines), [
            {"family": "Smith", "given": "A B"},
            {"family": "Jones", "given": "C D"},
        ])


class UniqlTest(unittest.TestCase):
    def test_dedups_and_drops_empty_family(self):
        self.assertEqual(
            md._uniq([{"family": "X", "given": "Y"}, {"family": "X", "given": "Y"},
                      {"family": "", "given": "Z"}, {"family": "W", "given": ""}]),
            [{"family": "X", "given": "Y"}, {"family": "W", "given": ""}],
        )


class ExtractMetadataTest(unittest.TestCase):
    def test_reads_first_page_and_owns_item(self):
        from unittest import mock
        with mock.patch("litcli.metadata.pdfutil.extract_text",
                        return_value="A Great Title Here\nA B Author, C D Author2\n2020") as m:
            item = md.extract_metadata("/tmp/x.pdf")
        m.assert_called_once_with("/tmp/x.pdf", first_page=True)
        self.assertEqual(item.source, "pdf")
        self.assertEqual(item.title, "A Great Title Here")


if __name__ == "__main__":
    unittest.main()
