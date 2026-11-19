"""Result ordering and filter enforcement inside ``search``.

Backend APIs are not a guarantee: a filter one source ignores must not leak records, and the
rows under ``--limit`` must be the ones that answer the query rather than whoever answered first.
"""
import argparse
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from litcli.commands import search
from litcli.commands.search import matches, relevance
from litcli.models import AcademicItem


def _item(title="T", **kw):
    base = dict(source="openalex", key=kw.pop("key", "10.1/x"), title=title, year="2020")
    base.update(kw)
    return AcademicItem(**base)


class MatchesTest(unittest.TestCase):
    def test_title_is_a_substring_match_either_case(self):
        it = _item("Graph Neural Networks")
        self.assertTrue(matches(it, {"title": "neural"}))
        self.assertFalse(matches(it, {"title": "transformer"}))

    def test_author_matches_given_or_family(self):
        it = _item(creators=[{"family": "LeCun", "given": "Yann"}])
        self.assertTrue(matches(it, {"author": "lecun"}))
        self.assertTrue(matches(it, {"author": "yann"}))
        self.assertFalse(matches(it, {"author": "hinton"}))

    def test_year_is_a_prefix_match(self):
        it = _item(year="2021-05")
        self.assertTrue(matches(it, {"year": "2021"}))
        self.assertFalse(matches(it, {"year": "2019"}))

    def test_container_and_no_filter(self):
        self.assertTrue(matches(_item(container_title="Nature"), {"container": "nature"}))
        self.assertTrue(matches(_item(), {}))


class RelevanceTest(unittest.TestCase):
    def test_a_title_hit_beats_an_abstract_hit(self):
        title = _item("Graph neural networks")
        abstract = _item("Something else", abstract="graph neural networks at scale")
        self.assertLess(relevance(title, "graph neural networks"),
                        relevance(abstract, "graph neural networks"))

    def test_all_tokens_in_the_title_wins(self):
        exact = _item("Graph neural networks for CFD")
        partial = _item("Neural networks", abstract="graph")
        self.assertLess(relevance(exact, "graph neural networks"), relevance(partial, "graph neural networks"))

    def test_citations_break_a_tie(self):
        weak = _item("Graph analysis")
        strong = _item("Graph analysis", cited_by_count=90)
        self.assertLess(relevance(strong, "graph"), relevance(weak, "graph"))


class SearchOrderTest(unittest.TestCase):
    def _run(self, items, argv):
        args = argparse.Namespace(backend="openalex", query="graph neural networks", limit=2,
                                 format="lines", no_cite=True, type=None, sort=None,
                                 title=None, author=None, year=None, container=None)
        for k, v in argv.items():
            setattr(args, k, v)
        seen = []
        backend = mock.Mock()
        backend.name = "openalex"
        backend.search.return_value = items
        with mock.patch.object(search, "get_backend", return_value=backend), \
                mock.patch.object(search, "print_items", side_effect=lambda its, *a: seen.extend(its)):
            search.cmd_search({}, args)
        return seen

    def test_the_limit_keeps_the_relevant_rows(self):
        off, on = _item("Off topic", abstract="graph"), _item("Graph neural networks")
        seen = self._run([off, on], {"limit": 1})
        self.assertEqual([it.title for it in seen], ["Graph neural networks"])

    def test_a_filter_the_backend_ignored_is_still_enforced(self):
        seen = self._run([_item("Graph neural networks"), _item("Graph databases")],
                         {"title": "neural"})
        self.assertEqual([it.title for it in seen], ["Graph neural networks"])

    def test_sort_cited_is_not_reordered_by_relevance(self):
        old = _item("Something else", cited_by_count=500)
        new = _item("Graph neural networks")
        seen = self._run([new, old], {"sort": "cited", "limit": 2})
        self.assertEqual([it.cited_by_count for it in seen], [500, 0])


if __name__ == "__main__":
    unittest.main()
