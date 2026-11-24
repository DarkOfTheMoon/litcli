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
from litcli.completions import BACKENDS
from litcli.publishers import _PUBLISHER_SPECS, PUBLISHER_ORDER


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

    def test_one_broken_reader_leaves_the_rest_of_the_table_usable(self):
        # The table is static data and its normalizers resolve the reader lazily: a module that
        # cannot be imported must cost that one step, not the whole table.
        from unittest import mock

        from litcli import publishers

        real = publishers._module
        with mock.patch.object(publishers, "_module",
                               lambda name: None if name == "ieee" else real(name)):
            acm = next(s for s in _PUBLISHER_SPECS if s.name == "acm")
            ieee = next(s for s in _PUBLISHER_SPECS if s.name == "ieee")
            self.assertEqual(acm.normalize("10.1145/3290605"), "doi:10.1145/3290605")
            # The broken reader owns nothing (its rules live in the module): empty, not a raise.
            self.assertEqual(ieee.normalize("10.1109/x.2020.1"), "")
            self.assertEqual(ieee.page_key("https://ieeexplore.ieee.org/document/1"), "")
            self.assertEqual(PUBLISHER_ORDER, tuple(s.name for s in _PUBLISHER_SPECS))

    def _run_with_a_broken_reader(self, setup: str):
        """Import the CLI with ``litcli.backends.ieee`` broken by ``setup`` in a fresh process."""
        import subprocess

        code = setup + (
            "import litcli.cli, litcli.fulltext, litcli.completions, litcli.commands.fetch\n"
            "from litcli import config\n"
            "assert 'ieee' not in config._registry(), 'the broken reader stayed registered'\n"
            "raise SystemExit(litcli.cli.main(['--help']))\n")
        return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                              cwd=os.path.join(os.path.dirname(__file__), ".."))

    def test_a_missing_reader_does_not_break_the_cli(self):
        # The file header promises Zotero alone keeps working; no reader is imported at module
        # level, so one that cannot be imported must not traceback `litcli --help` or the
        # registry every command builds.
        out = self._run_with_a_broken_reader("import sys; sys.modules['litcli.backends.ieee'] = None\n")
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_a_reader_that_raises_at_import_does_not_break_the_cli(self):
        # "Not ready" is not only ``ImportError``: a syntax error, a bad constant or a renamed
        # attribute all used to escape the guard and take the registry down with them.
        setup = (
            "import importlib.abc, importlib.machinery, sys\n"
            "class Boom(importlib.abc.MetaPathFinder, importlib.abc.Loader):\n"
            "    def find_spec(self, name, path=None, target=None):\n"
            "        if name == 'litcli.backends.ieee':\n"
            "            return importlib.machinery.ModuleSpec(name, self)\n"
            "    def create_module(self, spec):\n"
            "        return None\n"
            "    def exec_module(self, module):\n"
            "        raise ValueError('bad constant in reader')\n"
            "sys.meta_path.insert(0, Boom())\n")
        out = self._run_with_a_broken_reader(setup)
        self.assertEqual(out.returncode, 0, out.stderr)


if __name__ == "__main__":
    unittest.main()
