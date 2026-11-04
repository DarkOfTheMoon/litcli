"""Unit tests for the capability/`_filters` helpers on ``Backend``."""
import unittest

from litcli.backends import base
from litcli.backends.base import Backend


class _StubBackend(Backend):
    name = "stub"

    def search(self, query, limit=10, filters=None):
        return []

    def get(self, key):
        raise KeyError(key)


class BaseBackendTest(unittest.TestCase):
    def test_filters_coerces_int_year_to_str(self):
        # _filters must stringify an int year (or .isdigit() would crash on a raw int).
        out = Backend._filters({"year": 2018, "title": "T", "author": None, "container": ""})
        self.assertEqual(out, {"year": "2018", "title": "T"})
        self.assertIsInstance(out["year"], str)

    def test_filters_never_mutates_input(self):
        src = {"title": "T"}
        Backend._filters(src)
        self.assertEqual(src, {"title": "T"})

    def test_can_and_require_write(self):
        self.assertTrue(_StubBackend().can("read"))
        self.assertFalse(_StubBackend().can("write"))

    def test_require_write_raises_not_supported(self):
        with self.assertRaises(base.NotSupportedError):
            _StubBackend()._require("write")


if __name__ == "__main__":
    unittest.main()
