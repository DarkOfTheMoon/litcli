"""One expansion hop: every graph axis, each inside its own envelope.

The envelope is per axis, because the three axes cost differently and carry differently: a
``refs`` walk is a foundation hunt, a ``citing`` walk a frontier hunt, a ``related`` walk a
noise source. A backend that cannot do an axis (a local store) must cost only that axis.
"""
import os
import sys
import unittest
from typing import ClassVar

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from litcli import expansion
from litcli.backends.base import NotSupportedError
from litcli.errors import Unavailable
from litcli.models import AcademicItem

CAPS = {"refs": 3, "citing": 2, "related": 1}


def item(axis: str, n: int) -> AcademicItem:
    # A DOI, because that is the identity every graph source can be asked about.
    return AcademicItem(source="openalex", key=f"{axis}{n}", title=f"{axis} {n}",
                        doi=f"10.1/{axis}{n}")


class FakeBackend:
    name = "openalex"
    capabilities: ClassVar[set[str]] = {"read", "refs", "citing", "related"}

    def __init__(self):
        self.calls: list[tuple[str, int]] = []
        self.unsupported: set[str] = set()
        self.counts = {"refs": 3, "citing": 2, "related": 1}

    def _answer(self, axis, limit):
        self.calls.append((axis, limit))
        if axis in self.unsupported:
            raise NotSupportedError(f"{axis} not supported", self)
        return [item(axis, n) for n in range(self.counts[axis])]

    def references(self, key, limit=5):
        return self._answer("refs", limit)

    def citing(self, key, limit=5):
        return self._answer("citing", limit)

    def related(self, key, limit=5):
        return self._answer("related", limit)


def expand_all(backend, counters):
    return list(expansion.expand(item("seed", 0), backend, CAPS, counters))


class AxisTest(unittest.TestCase):
    def test_every_axis_is_walked_and_labelled(self):
        backend = FakeBackend()
        out = expand_all(backend, dict(CAPS))
        self.assertEqual([axis for _item, axis in out],
                         ["refs", "refs", "refs", "citing", "citing", "related"])

    def test_the_axis_limit_is_the_envelope(self):
        backend = FakeBackend()
        expand_all(backend, dict(CAPS))
        self.assertEqual(backend.calls, [("refs", 3), ("citing", 2), ("related", 1)])

    def test_an_exhausted_axis_is_not_called(self):
        backend = FakeBackend()
        expand_all(backend, {"refs": 0, "citing": 0, "related": 1})
        self.assertEqual(backend.calls, [("related", 1)])

    def test_an_unsupported_axis_costs_only_that_axis(self):
        backend = FakeBackend()
        backend.unsupported = {"citing"}
        out = expand_all(backend, dict(CAPS))
        self.assertEqual([axis for _item, axis in out],
                         ["refs", "refs", "refs", "related"])
        self.assertEqual(backend.calls, [("refs", 3), ("citing", 2), ("related", 1)])


class CounterTest(unittest.TestCase):
    def test_the_envelope_shrinks_by_what_was_taken(self):
        counters = dict(CAPS)
        backend = FakeBackend()
        backend.counts["refs"] = 5  # more than the ask: the ask still bounds it
        expand_all(backend, counters)
        self.assertEqual(counters, {"refs": 0, "citing": 0, "related": 0})

    def test_only_yielded_items_are_charged(self):
        backend = FakeBackend()
        backend.counts["refs"] = 1  # asks for 3, gets 1
        counters = dict(CAPS)
        expand_all(backend, counters)
        self.assertEqual(counters["refs"], 2)

    def test_an_upper_bound_caps_the_ask(self):
        backend = FakeBackend()
        backend.counts["refs"] = 1
        expand_all(backend, {"refs": 1, "citing": 0, "related": 0})
        self.assertEqual(backend.calls, [("refs", 1)])


class SourceSpy(FakeBackend):
    """A second graph source: same shape, its own name and capability declaration."""

    def __init__(self, name="semanticscholar", axes=("refs", "citing", "related")):
        super().__init__()
        self.name = name
        self.capabilities = {"read", *axes}


class DupeSource(FakeBackend):
    name = "opencitations"

    def references(self, key, limit=5):
        self.calls.append(("refs", limit))
        return [item("refs", 0), item("refs", 1)]  # same identities as FakeBackend's first two


class MultiSourceTest(unittest.TestCase):
    def test_sources_are_asked_in_order_for_the_room_left(self):
        first, second = FakeBackend(), SourceSpy()
        first.counts = {"refs": 1, "citing": 0, "related": 0}
        second.counts = {"refs": 3, "citing": 0, "related": 0}
        out = list(expansion.expand(item("seed", 0), [first, second], CAPS, dict(CAPS)))
        self.assertEqual([it.key for it, _a in out], ["refs0", "refs1", "refs2"])
        self.assertEqual([c for c in first.calls if c[0] == "refs"], [("refs", 3)])
        self.assertEqual([c for c in second.calls if c[0] == "refs"], [("refs", 2)])  # what was left

    def test_the_same_work_from_two_sources_is_yielded_once(self):
        first, dupes = FakeBackend(), DupeSource()
        first.counts = {"refs": 3, "citing": 0, "related": 0}
        dupes.counts = {"refs": 2, "citing": 0, "related": 0}
        out = list(expansion.expand(item("seed", 0), [first, dupes], CAPS, dict(CAPS)))
        self.assertEqual([it.key for it, _a in out], ["refs0", "refs1", "refs2"])

    def test_a_source_without_the_axis_is_never_asked(self):
        meta = SourceSpy(name="crossref", axes=("refs",))
        meta.calls.clear()
        out = list(expansion.expand(item("seed", 0), [meta], {"refs": 3, "citing": 2, "related": 1},
                                    dict(CAPS)))
        self.assertEqual([axis for _i, axis in out], ["refs", "refs", "refs"])
        self.assertEqual([c[0] for c in meta.calls], ["refs"])


class Walled(SourceSpy):
    """A source that is throttled or out of budget: it raises on every axis."""

    name = "openalex"

    def _answer(self, axis, limit):
        self.calls.append((axis, limit))
        raise Unavailable("[openalex] HTTP 429: Too Many Requests")


class MisdeclaredSource:
    """Declares ``refs`` and never implemented it: the walk must not fall over it."""

    name = "semanticscholar"
    capabilities = frozenset({"read", "refs"})


class DeclaredWithoutMethodTest(unittest.TestCase):
    def test_a_declared_but_missing_method_does_not_end_the_hop(self):
        good = SourceSpy()
        good.counts = {"refs": 1, "citing": 0, "related": 0}
        good.capabilities = {"read", "refs"}
        out = list(expansion.expand(item("seed", 0), [MisdeclaredSource(), good], CAPS,
                                    dict(CAPS)))
        self.assertEqual([it.key for it, _a in out], ["refs0"])

    def test_the_same_source_would_have_crashed_the_hop_before(self):
        # Belt and braces: a source handed straight to ``expand`` (bypassing the gate) is
        # skipped per axis, not raised out of the whole row.
        # Alone, it is still a failed hop (the row must be retried) — but an AttributeError,
        # which the walk now catches, not an escape out of the run.
        with self.assertRaises(AttributeError):
            list(expansion.expand(item("seed", 0), [MisdeclaredSource()], CAPS, dict(CAPS)))


class TargetTest(unittest.TestCase):
    """Each source is addressed in its own spelling, and skipped when it has none."""

    def test_each_source_gets_the_spelling_it_understands(self):
        seed = item("seed", 0)
        oa = type("B", (), {"name": "openalex"})()
        s2 = type("B", (), {"name": "semanticscholar"})()
        bare = type("B", (), {"name": "opencitations"})()
        self.assertEqual(expansion.target_for(oa, seed), "doi:10.1/seed0")
        self.assertEqual(expansion.target_for(s2, seed), "DOI:10.1/seed0")
        self.assertEqual(expansion.target_for(bare, seed), "10.1/seed0")

    def test_a_doi_only_source_is_not_asked_about_a_doi_less_work(self):
        # A planted request that can only 404 costs a round trip and a 429 budget unit.
        no_doi = AcademicItem(source="europepmc", key="PMC7654321", title="T")
        bare = type("B", (), {"name": "opencitations"})()
        cross = type("B", (), {"name": "crossref"})()
        self.assertIsNone(expansion.target_for(bare, no_doi))
        self.assertIsNone(expansion.target_for(cross, no_doi))

    def test_an_identifier_addressable_source_still_gets_the_key(self):
        no_doi = AcademicItem(source="europepmc", key="PMC7654321", title="T")
        epmc = type("B", (), {"name": "europepmc"})()
        self.assertEqual(expansion.target_for(epmc, no_doi), "PMC7654321")


class FallbackTest(unittest.TestCase):
    """One spent source costs its own answer, never the axis or the hop."""

    def test_a_walled_source_hands_the_axis_to_the_next_one(self):
        walled, good = Walled(), SourceSpy()
        good.counts = {"refs": 2, "citing": 1, "related": 1}
        out = list(expansion.expand(item("seed", 0), [walled, good], CAPS, dict(CAPS)))
        self.assertEqual([(it.key, axis) for it, axis in out],
                         [("refs0", "refs"), ("refs1", "refs"), ("citing0", "citing"),
                          ("related0", "related")])

    def test_the_room_a_wall_burned_is_not_charged(self):
        walled = Walled()
        counters = dict(CAPS)
        with self.assertRaises(Unavailable):
            list(expansion.expand(item("seed", 0), [walled], CAPS, counters))
        self.assertEqual(counters, CAPS)

    def test_a_row_where_every_source_walled_is_not_a_successful_expansion(self):
        # The walk retries an errored row; a swallowed wall would mark it expanded for good.
        with self.assertRaises(Unavailable):
            list(expansion.expand(item("seed", 0), [Walled(), Walled()], CAPS, dict(CAPS)))

    def test_the_first_wall_is_the_reason_reported(self):
        class Second(Walled):
            def _answer(self, axis, limit):
                raise Unavailable("second down")

        with self.assertRaises(Unavailable) as caught:
            list(expansion.expand(item("seed", 0), [Walled(), Second()], CAPS, dict(CAPS)))
        self.assertIn("429", str(caught.exception))

    def test_a_wall_does_not_hide_the_axes_the_next_source_answers(self):
        walled, good = Walled(), SourceSpy(axes=("related",))
        good.counts = {"refs": 0, "citing": 0, "related": 1}
        out = list(expansion.expand(item("seed", 0), [walled, good], CAPS, dict(CAPS)))
        self.assertEqual([axis for _i, axis in out], ["related"])
    """A link service: DOIs, no metadata (OpenCitations is this shape)."""

    name = "opencitations"

    def references(self, key, limit=5):
        self.calls.append(("refs", limit))
        return [AcademicItem(source=self.name, key="10.1/refs0", doi="10.1/refs0")]


class LinkOnlySource(FakeBackend):
    """A link service: DOIs, no metadata (OpenCitations is this shape)."""

    name = "opencitations"

    def references(self, key, limit=5):
        self.calls.append(("refs", limit))
        return [AcademicItem(source=self.name, key="10.1/refs0", doi="10.1/refs0")]


class MetadataSource(SourceSpy):
    name = "crossref"

    def get(self, key):
        return AcademicItem(source=self.name, key=key, doi=key, title="Hydrated title")


class HydrationTest(unittest.TestCase):
    def test_a_bare_link_candidate_borrows_its_metadata(self):
        # A link service answers DOIs only; without a title the scorer sees nothing to score.
        link, meta = LinkOnlySource(), MetadataSource()
        meta.counts = {"refs": 0, "citing": 0, "related": 0}
        out = list(expansion.expand(item("seed", 0), [link, meta], {"refs": 1}, {"refs": 1}))
        self.assertEqual([it.title for it, _a in out], ["Hydrated title"])

    def test_a_failed_hydration_keeps_the_bare_item(self):
        link, meta = LinkOnlySource(), MetadataSource()
        meta.get = lambda key: (_ for _ in ()).throw(RuntimeError("down"))
        out = list(expansion.expand(item("seed", 0), [link, meta], {"refs": 1}, {"refs": 1}))
        self.assertEqual([(it.key, it.title) for it, _a in out], [("10.1/refs0", "")])
