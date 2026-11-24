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
from litcli.models import AcademicItem

CAPS = {"refs": 3, "citing": 2, "related": 1}


def item(axis: str, n: int) -> AcademicItem:
    return AcademicItem(source="openalex", key=f"{axis}{n}", title=f"{axis} {n}")


class FakeBackend:
    name = "openalex"
    capabilities: ClassVar[set[str]] = {"read"}

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
