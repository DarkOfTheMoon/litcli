"""``related`` fallback: an empty graph list must not read as "nothing close exists"."""
import argparse
import contextlib
import io
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from litcli.commands import graph
from litcli.commands.graph import _keywords
from litcli.models import AcademicItem


class _Backend:
    name = "openalex"

    def __init__(self, related=(), hits=()):
        self._related = list(related)
        self._hits = list(hits)
        self.searched = None

    def related(self, key, limit=5, sort_cited=False):
        return self._related

    def get(self, key):
        return AcademicItem(source="openalex", key=key, title="Graph neural networks for CFD",
                            doi="10.1/seed")

    def search(self, query, limit=10, filters=None):
        self.searched = query
        return list(self._hits)


def _args(**over):
    base = dict(backend="openalex", key="10.1/seed", limit=3, format="lines", no_cite=True,
                sort=None)
    base.update(over)
    return argparse.Namespace(**base)


def _run(backend):
    seen = []
    err = io.StringIO()
    with mock.patch.object(graph, "resolve_backend", return_value=(backend, "10.1/seed")), \
            mock.patch.object(graph, "print_items", side_effect=lambda its, *a: seen.extend(its)), \
            contextlib.redirect_stderr(err):
        graph.cmd_edge({}, _args(), "related")
    return seen, err.getvalue()


class KeywordTest(unittest.TestCase):
    def test_stopwords_are_dropped_and_long_words_come_first(self):
        self.assertEqual(_keywords("The graph of neural networks")[:2], ["networks", "neural"])


class RelatedFallbackTest(unittest.TestCase):
    def test_an_empty_graph_falls_back_to_the_title(self):
        b = _Backend(hits=[AcademicItem(source="openalex", key="10.1/a", title="CNNs"),
                           AcademicItem(source="openalex", key="10.1/b", title="GNNs")])
        seen, err = _run(b)
        self.assertIsNotNone(b.searched)
        self.assertIn("cfd", b.searched)
        self.assertEqual([it.key for it in seen], ["10.1/a", "10.1/b"])
        self.assertTrue(all(it.extra["related_via"] == "keyword" for it in seen))
        self.assertIn("matching on the title", err)

    def test_the_seed_is_not_returned_as_its_own_neighbour(self):
        same = AcademicItem(source="openalex", key="10.1/seed", title="Seed", doi="10.1/seed")
        seen, _ = _run(_Backend(hits=[same]))
        self.assertEqual(seen, [])

    def test_a_non_empty_graph_is_left_alone(self):
        b = _Backend(related=[AcademicItem(source="openalex", key="10.1/r", title="R")])
        seen, err = _run(b)
        self.assertIsNone(b.searched)
        self.assertEqual(err, "")
        self.assertEqual([it.key for it in seen], ["10.1/r"])

    def test_the_fallback_respects_the_limit(self):
        b = _Backend(hits=[AcademicItem(source="openalex", key=f"10.1/{i}", title="T")
                           for i in range(6)])
        seen, _ = _run(b)
        self.assertEqual(len(seen), 3)


if __name__ == "__main__":
    unittest.main()
