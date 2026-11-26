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

    def test_a_limit_of_zero_does_not_empty_the_fallback(self):
        # `--limit 0` means "no cap" here as everywhere else; the fallback was handed the raw
        # 0 and sliced its own matches away.
        backend = _Backend(related=[], hits=[AcademicItem(key="10.1/n", doi="10.1/n",
                                                          title="Neighbour")])
        err = io.StringIO()
        seen = []
        with mock.patch.object(graph, "resolve_backend", return_value=(backend, "10.1/seed")), \
                mock.patch.object(graph, "print_items",
                                  side_effect=lambda its, *a: seen.extend(its)), \
                contextlib.redirect_stderr(err):
            graph.cmd_edge({}, _args(limit=0), "related")
        self.assertEqual([i.key for i in seen], ["10.1/n"])

    def test_an_unknown_pinned_backend_is_bad_input(self):
        # `resolve_backend` raises a bare KeyError / RuntimeError; uncaught it read as an
        # "unexpected" traceback rather than the user's own typo in `--backend`.
        from litcli.errors import BadInput

        with mock.patch.object(graph, "resolve_backend", side_effect=KeyError("nope")), \
                mock.patch.object(graph, "print_items"), self.assertRaises(BadInput) as caught:
            graph.cmd_edge({}, _args(backend="nope"), "references")
        self.assertIn("nope", str(caught.exception))


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

    def citing(self, key, limit=5, sort_cited=True):
        self.asked = key
        return list(self.items)

    def related(self, key, limit=5, sort_cited=False):
        self.asked = key
        return list(self.items)


class _LinkSource:
    """A DOI-pair service: answers the edge with bare DOIs and carries no metadata itself."""

    name = "opencitations"

    def __init__(self, dois):
        self.dois = dois
        self.capabilities = {"refs"}  # like COCI: no metadata to read
        self.asked = None

    def references(self, key, limit=5, sort_cited=True):
        self.asked = key
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
                            year="2019", cited_by_count=500)


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


class _BareSource:
    """A link service that *also* has a ``get``: the shape a hydration pool is built for."""

    capabilities = frozenset({"read", "refs"})

    def __init__(self, name="opencitations", dois=("10.1/x",)):
        self.name = name
        self.dois = list(dois)
        self.asked = []

    def references(self, key, limit=5, sort_cited=True):
        return [AcademicItem(source=self.name, key=d, doi=d) for d in self.dois][:limit]

    title = "A real title"

    def get(self, key):
        self.asked.append(key)
        return AcademicItem(source=self.name, key=key, doi=key, title=self.title,
                            cited_by_count=50)


class HydrationPoolTest(unittest.TestCase):
    """The pool may name the same source twice; it must be asked once."""

    def _run(self, pinned, pool):
        seen = []
        with mock.patch.object(graph, "graph_sources", return_value=pool), \
                mock.patch.object(graph, "resolve_backend", return_value=(pinned, "10.1/seed")), \
                mock.patch.object(graph, "print_items",
                                  side_effect=lambda its, *a: seen.extend(its) or 0), \
                contextlib.redirect_stderr(io.StringIO()):
            graph.cmd_edge({}, _args(backend="opencitations"), "references")
        return seen

    def test_a_source_named_twice_in_the_pool_is_asked_once(self):
        # graph_sources builds fresh instances, so the pinned one never compares equal by
        # identity to its own twin: the pool has to be deduped by name. Both twins are the
        # same service, so the twin adds no answer — only a second round trip.
        pinned = _BareSource()
        pinned.title = ""  # this instance cannot fill the row, or the first success hides it
        twin = _BareSource()
        self._run(pinned, [twin])
        self.assertEqual(pinned.asked + twin.asked, ["10.1/x"])


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
        self.assertEqual(good.asked, "DOI:10.1/seed")
        self.assertIn("[skipped] openalex", err)  # never silent
        self.assertEqual(rc, 0)

    def test_an_unreadable_answer_does_not_kill_the_command(self):
        # A 200 with a JSON list (``data.get`` on a list) and a 200 with an empty body (a bare
        # KeyError) are how the backends report one unreadable answer; both used to escape the
        # per-source handler and end the command on a traceback instead of falling through.
        good = _GraphSource([AcademicItem(key="10.1/x", doi="10.1/x")])
        for exc in (AttributeError("'list' object has no attribute 'get'"), KeyError("W1")):
            with self.subTest(type(exc).__name__):
                rc, seen, err = self._run([_Raising(exc), good])
                self.assertEqual([i.key for i in seen], ["10.1/x"])
                self.assertIn("[skipped] openalex", err)
                self.assertEqual(rc, 0)

    def test_a_source_without_a_spelling_for_the_key_is_not_asked(self):
        # A W-id is an OpenAlex spelling; COCI and Crossref address works by DOI only, so
        # asking them can only 404 — a wasted request and a spurious "[skipped]" line.
        first = _GraphSource([AcademicItem(key="10.1/a", doi="10.1/a", title="A")])
        first.name = "openalex"
        link = _LinkSource(["10.1/b"])
        seen, err = [], io.StringIO()
        with mock.patch.object(graph, "graph_sources", return_value=[first, link]), \
                mock.patch.object(graph, "print_items",
                                  side_effect=lambda its, *a: seen.extend(its) or 0), \
                contextlib.redirect_stderr(err):
            graph.cmd_edge({}, _args(backend=None, key="W123"), "references")
        self.assertIsNone(link.asked)
        self.assertEqual([i.key for i in seen], ["10.1/a"])
        self.assertNotIn("skipped", err.getvalue())

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


class _Counting(_GraphSource):
    """A source whose rows carry citation counts, so ordering can be observed."""

    def __init__(self, rows, name="semanticscholar"):
        super().__init__([AcademicItem(source=name, key=k, doi=k, title=k, cited_by_count=c)
                          for k, c in rows])
        self.name = name


class GraphUnionTest(unittest.TestCase):
    """``refs``/``related`` are a neighbourhood hunt: every source that answers adds to it."""

    def _run(self, sources, method="references", **over):
        seen = []
        err = io.StringIO()
        args = _args(backend=None, **over)
        with mock.patch.object(graph, "graph_sources", return_value=sources), \
                mock.patch.object(graph, "print_items",
                                  side_effect=lambda its, *a: seen.extend(its) or 0), \
                contextlib.redirect_stderr(err):
            rc = graph.cmd_edge({}, args, method)
        return rc, seen, err.getvalue()

    def test_refs_takes_the_other_sources_too(self):
        first = _GraphSource([AcademicItem(key="10.1/a", doi="10.1/a", title="A")])
        second = _Counting([("10.1/b", 3), ("10.1/c", 1)])
        _rc, seen, _err = self._run([first, second])
        self.assertEqual([i.key for i in seen], ["10.1/a", "10.1/b", "10.1/c"])
        self.assertEqual(second.asked, "DOI:10.1/seed")

    def test_citing_keeps_the_first_answer(self):
        first = _GraphSource([AcademicItem(key="10.1/a", doi="10.1/a", title="A")])
        second = _Counting([("10.1/b", 3)])
        _rc, seen, _err = self._run([first, second], method="citing")
        self.assertEqual([i.key for i in seen], ["10.1/a"])
        self.assertIsNone(second.asked)

    def test_a_source_missing_one_declared_axis_still_answers_the_others(self):
        # Declaring two axes and implementing one is a half-done source, not a broken one:
        # both callers ask axis by axis and fall through, so the working axis must stay usable.
        from litcli.commands import keys

        partial = _GraphSource([])
        partial.capabilities = {"refs", "citing", "related"}
        partial.citing = None  # declared, but nothing behind it
        with mock.patch("litcli.commands.keys.get_all_backends",
                        lambda cfg: {"semanticscholar": partial}), \
                mock.patch("litcli.commands.keys.optional_backend", lambda name, cfg: None):
            out = keys.graph_sources({})
        self.assertIn(partial, out)

    def test_a_pinned_backend_does_not_pull_in_the_others(self):
        first = _GraphSource([AcademicItem(key="10.1/a", doi="10.1/a", title="A")])
        other = _Counting([("10.1/b", 3)])
        seen = []
        with mock.patch.object(graph, "graph_sources", return_value=[first, other]), \
                mock.patch.object(graph, "resolve_backend", return_value=(first, "10.1/seed")), \
                mock.patch.object(graph, "print_items",
                                  side_effect=lambda its, *a: seen.extend(its) or 0), \
                contextlib.redirect_stderr(io.StringIO()):
            graph.cmd_edge({}, _args(backend="openalex"), "references")
        self.assertEqual([i.key for i in seen], ["10.1/a"])
        self.assertIsNone(other.asked)

    def test_a_work_from_two_sources_appears_once(self):
        same = AcademicItem(key="10.1/a", doi="10.1/a", title="A")
        first = _GraphSource([same])
        second = _GraphSource([AcademicItem(key="10.1/a", doi="10.1/a", title="A"),
                               AcademicItem(key="10.1/b", doi="10.1/b", title="B")])
        _rc, seen, _err = self._run([first, second])
        self.assertEqual([i.key for i in seen], ["10.1/a", "10.1/b"])

    def test_extra_rows_come_after_the_base_and_best_cited_first(self):
        base = _Counting([("10.1/t", 1)], name="openalex")
        second = _Counting([("10.1/u2", 0), ("10.1/u3", 5), ("10.1/u1", 9)])
        _rc, seen, _err = self._run([base, second], limit=4)
        # the first source's table order is the reader's expectation; extras sort among themselves
        self.assertEqual([i.key for i in seen], ["10.1/t", "10.1/u1", "10.1/u3", "10.1/u2"])

    def test_cited_sort_orders_the_merged_rows_and_puts_uncited_last(self):
        base = _Counting([("10.1/t", 1)], name="openalex")
        second = _Counting([("10.1/u", 9), ("10.1/n", 0)])
        _rc, seen, _err = self._run([base, second], sort="cited")
        self.assertEqual([i.key for i in seen], ["10.1/u", "10.1/t", "10.1/n"])

    def test_the_limit_caps_the_union(self):
        first = _GraphSource([AcademicItem(key="10.1/a", doi="10.1/a", title="A")])
        second = _Counting([("10.1/b", 3), ("10.1/c", 1)])
        _rc, seen, _err = self._run([first, second], limit=2)
        self.assertEqual([i.key for i in seen], ["10.1/a", "10.1/b"])

    def test_cited_sort_ranks_hydrated_rows_by_their_real_count(self):
        # Hydration fills `cited_by_count`, so a sort taken before it ranks a whole link-source
        # block as zero and buries it — the number the reader asked to sort by exists only
        # after the fill.
        base = _Counting([("10.1/t", 1)], name="openalex")
        link = _LinkSource(["10.1/y"])
        meta = _MetadataSource()
        _rc, seen, _err = self._run([base, link, meta], sort="cited")
        self.assertEqual([i.key for i in seen], ["10.1/y", "10.1/t"])

    def test_a_zero_limit_is_no_cap_like_everywhere_else(self):
        # `search --limit 0` means "no cap"; the union path used to break on `0 >= 0` before
        # asking anyone and print an empty table.
        first = _GraphSource([AcademicItem(key="10.1/a", doi="10.1/a", title="A")])
        second = _Counting([("10.1/b", 3)])
        _rc, seen, _err = self._run([first, second], limit=0)
        self.assertTrue(seen)

    def test_a_source_that_overshoots_the_limit_is_truncated(self):
        rows = [AcademicItem(key=f"10.1/{i}", doi=f"10.1/{i}", title="T") for i in range(9)]
        _rc, seen, _err = self._run([_GraphSource(rows)], limit=4)
        self.assertEqual(len(seen), 4)

    def test_a_pinned_source_with_cited_sort_comes_out_sorted(self):
        # Pinned runs have no union merge to sort them, and the backends that ignore
        # `sort_cited` would otherwise hand back their natural order.
        rows = _Counting([("10.1/low", 1), ("10.1/high", 40)])
        seen = []
        with mock.patch.object(graph, "resolve_backend", return_value=(rows, "10.1/seed")), \
                mock.patch.object(graph, "print_items",
                                  side_effect=lambda its, *a: seen.extend(its) or 0), \
                contextlib.redirect_stderr(io.StringIO()):
            graph.cmd_edge({}, _args(backend="semanticscholar", sort="cited"), "references")
        self.assertEqual([i.key for i in seen], ["10.1/high", "10.1/low"])

    def test_a_wall_on_an_extra_source_keeps_the_base_rows(self):
        from litcli.errors import Unavailable

        first = _GraphSource([AcademicItem(key="10.1/a", doi="10.1/a", title="A")])
        rc, seen, err = self._run([first, _Raising(Unavailable("[semanticscholar] 429"))])
        self.assertEqual([i.key for i in seen], ["10.1/a"])
        self.assertIn("[skipped] openalex", err)
        self.assertEqual(rc, 0)
