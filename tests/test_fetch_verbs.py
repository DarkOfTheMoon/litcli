"""``fetch`` still understands the 0.1 grammar, which stale skill copies keep teaching.

``fetch fulltext <doi>`` / ``fetch book <title>`` / ``--download`` used to be the only forms;
letting them fail with a bare argparse usage costs the reader the whole command.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from litcli.commands import fetch
from litcli.commands.parser import build_parser


class ParseTest(unittest.TestCase):
    def test_the_old_verb_and_flag_still_parse(self):
        a = build_parser().parse_args(["fetch", "fulltext", "10.1/x", "--download"])
        self.assertEqual((a.target, a.extra, a.download), ("fulltext", ["10.1/x"], True))

    def test_an_unquoted_title_parses(self):
        a = build_parser().parse_args(["fetch", "--book", "finite", "volume", "method"])
        self.assertEqual(a.extra, ["volume", "method"])


class DispatchTest(unittest.TestCase):
    def _run(self, argv):
        args = build_parser().parse_args(argv)
        full, book = mock.Mock(return_value=0), mock.Mock(return_value=0)
        with mock.patch.object(fetch, "_fetch_fulltext", full), \
                mock.patch.object(fetch, "_fetch_book", book):
            fetch.cmd_fetch({}, args)
        return full, book

    def test_fulltext_is_a_verb_not_a_target(self):
        full, book = self._run(["fetch", "fulltext", "10.1/x"])
        self.assertEqual(full.call_args.args[2], "10.1/x")
        book.assert_not_called()

    def test_book_before_an_isbn_keeps_the_book_path(self):
        _full, book = self._run(["fetch", "book", "978-7-118-04834-8"])
        self.assertEqual(book.call_args.args[2], "978-7-118-04834-8")

    def test_extra_words_are_joined_back_into_the_target(self):
        full, _ = self._run(["fetch", "fulltext", "10.1/x", "extra"])
        self.assertEqual(full.call_args.args[2], "10.1/x extra")

    def test_a_bare_verb_reports_an_empty_target(self):
        full, _ = self._run(["fetch", "fulltext"])
        full.assert_not_called()


if __name__ == "__main__":
    unittest.main()
