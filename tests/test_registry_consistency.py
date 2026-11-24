"""The publisher table is the single source: chain order, registry and completion must agree.

A miss in either direction is a real bug — a reader registered (or offered by completion) with
no spec never enters the fulltext chain, and a spec naming a kind nobody registers is a dead
step. Keeping the three lists derived from one table makes these checks cheap and loud.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from litcli import config, fulltext
from litcli.backends._entitled import EntitledBackend
from litcli.backends.publishers import _PUBLISHER_SPECS, PUBLISHER_ORDER
from litcli.completions import BACKENDS


class PublisherTableTest(unittest.TestCase):
    def test_the_order_is_exactly_the_table(self):
        self.assertEqual(PUBLISHER_ORDER, tuple(spec.name for spec in _PUBLISHER_SPECS))
        self.assertEqual(fulltext.PUBLISHER_ORDER, PUBLISHER_ORDER)

    def test_every_spec_is_registered_and_completable(self):
        reg = config._registry()
        for spec in _PUBLISHER_SPECS:
            with self.subTest(spec.name):
                self.assertIn(spec.name, reg)
                self.assertIn(spec.name, BACKENDS)
                self.assertTrue(spec.hint.strip())
                self.assertTrue(spec.doi_re)
                self.assertTrue(spec.backend)

    def test_every_registered_entitled_backend_has_a_spec(self):
        names = {spec.name for spec in _PUBLISHER_SPECS}
        for name, cls in config._registry().items():
            if issubclass(cls, EntitledBackend):
                with self.subTest(name):
                    self.assertIn(name, names)

    def test_the_registered_class_is_the_specs_class(self):
        reg = config._registry()
        for spec in _PUBLISHER_SPECS:
            with self.subTest(spec.name):
                self.assertEqual(reg[spec.name].__name__, spec.backend)


if __name__ == "__main__":
    unittest.main()
