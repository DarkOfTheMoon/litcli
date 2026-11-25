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


class _Raising:
    name = "openalex"

    def __init__(self, exc):
        self.exc = exc

    def references(self, key, limit=5, sort_cited=True):
        raise self.exc


class _GraphSource:
    name = "semanticscholar"

    def __init__(self, items):
        self.items = items
        self.asked = None

    def references(self, key, limit=5, sort_cited=True):
        self.asked = key
        return list(self.items)


class _LinkSource:
    """A DOI-pair service: answers the edge with bare DOIs and carries no metadata itself."""

    name = "opencitations"

    def __init__(self, dois):
        self.dois = dois
        self.capabilities = {"read", "refs"}

    def references(self, key, limit=5, sort_cited=True):
        return [AcademicItem(source=self.name, key=d, doi=d) for d in self.dois][:limit]


class _MetadataSource:
    """What a bare DOI is hydrated from: it answers no axis, only ``get``."""

    name = "crossref"
    capabilities = frozenset({"read"})

    def __init__(self):
        self.asked = []

    def references(self, key, limit=5, sort_cited=True):
        raise graph.NotSupportedError("references", self)

    def get(self, key):
        self.asked.append(key)
        return AcademicItem(source=self.name, key=key, doi=key, title="A real title",
                            year="2019")


class PinnedHydrationTest(unittest.TestCase):
    """Pinning a link source must not leave the table full of bare DOIs."""

    def test_a_pinned_link_source_still_borrows_metadata(self):
        link, meta = _LinkSource(["10.1/x"]), _MetadataSource()
        seen = []
        with mock.patch.object(graph, "graph_sources", return_value=[link, meta]), \
                mock.patch.object(graph, "resolve_backend",
                                  return_value=(link, "10.1/seed")), \
                mock.patch.object(graph, "print_items",
                                  side_effect=lambda its, *a: seen.extend(its) or 0), \
                contextlib.redirect_stderr(io.StringIO()):
            graph.cmd_edge({}, _args(backend="opencitations"), "references")
        self.assertEqual([(i.key, i.title) for i in seen], [("10.1/x", "A real title")])
        self.assertEqual(meta.asked, ["10.1/x"])


class GraphFallbackTest(unittest.TestCase):
    """One spent source must not end an edge: the next one is asked, and it is said."""

    def _run(self, sources, method="references"):
        seen = []
        err = io.StringIO()
        with mock.patch.object(graph, "graph_sources", return_value=sources), \
                mock.patch.object(graph, "print_items",
                                  side_effect=lambda its, *a: seen.extend(its) or 0), \
                contextlib.redirect_stderr(err):
            rc = graph.cmd_edge({}, _args(backend=None), method)
        return rc, seen, err.getvalue()

    def test_a_throttled_first_source_falls_to_the_next(self):
        from litcli.errors import Unavailable

        good = _GraphSource([AcademicItem(source="semanticscholar", key="10.1/x", doi="10.1/x",
                                          title="A neighbour")])
        rc, seen, err = self._run([_Raising(Unavailable("[openalex] HTTP 429")), good])
        self.assertEqual([i.key for i in seen], ["10.1/x"])
        self.assertEqual(good.asked, "10.1/seed")
        self.assertIn("[skipped] openalex", err)  # never silent
        self.assertEqual(rc, 0)

    def test_an_empty_answer_still_lets_the_next_source_speak(self):
        empty, good = _GraphSource([]), _GraphSource([AcademicItem(key="10.1/x", doi="10.1/x")])
        rc, seen, _err = self._run([empty, good])
        self.assertEqual([i.key for i in seen], ["10.1/x"])
        self.assertEqual(rc, 0)

    def test_every_source_failing_raises_the_last_reason(self):
        from litcli.errors import Unavailable

        with self.assertRaises(Unavailable) as caught:
            self._run([_Raising(Unavailable("first")), _Raising(Unavailable("second"))])
        self.assertIn("second", str(caught.exception))

    def test_a_bare_doi_from_a_link_source_is_hydrated_before_it_is_shown(self):
        # COCI hands back DOIs only; a row without a title is not usable, so it is looked up.
        meta = _MetadataSource()
        rc, seen, _err = self._run([_LinkSource(["10.1/x"]), meta])
        self.assertEqual(meta.asked, ["10.1/x"])
        self.assertEqual([(i.key, i.title, i.year) for i in seen],
                         [("10.1/x", "A real title", "2019")])
        self.assertEqual(seen[0].source, "opencitations")  # the source that found it still shows
        self.assertEqual(rc, 0)

    def test_a_wall_outranks_a_not_found_when_everything_fails(self):
        # "the key is not here" from a bare DOI service explains less than "the source is spent".
        from litcli.errors import NotFound, Unavailable

        with self.assertRaises(Unavailable) as caught:
            self._run([_Raising(Unavailable("[openalex] HTTP 429")),
                       _Raising(NotFound("[semanticscholar] no such paper"))])
        self.assertIn("429", str(caught.exception))
