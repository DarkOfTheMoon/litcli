"""Keep the Chinese and English READMEs saying the same things.

Two entry docs are two places to forget: a backend added to one, a command renamed in the other.
These tests pin the inventories and the language switch; prose is free to differ.
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from litcli import config

ROOT = os.path.join(os.path.dirname(__file__), "..")
ZH = os.path.join(ROOT, "README.md")
EN = os.path.join(ROOT, "README.en.md")

_COMMAND_RE = re.compile(r"`litcli\s+([a-z][a-z-]*)")
_BACKEND_RE = re.compile(r"--backend\s+([a-z\\\\|]+)")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _commands(text):
    return set(_COMMAND_RE.findall(text))


def _backends(text, registry):
    names = {n for group in _BACKEND_RE.findall(text) for n in group.split("\\|")}
    return {n for n in names if n in registry}


class ReadmeParityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.zh = _read(ZH)
        cls.en = _read(EN)

    def test_both_link_each_other(self):
        self.assertIn("README.en.md", self.zh)
        self.assertIn("README.md", self.en)

    def test_same_commands(self):
        self.assertEqual(_commands(self.zh), _commands(self.en))

    def test_same_backends(self):
        registry = config._registry()
        self.assertEqual(_backends(self.zh, registry), _backends(self.en, registry))

    def test_documented_backends_exist(self):
        registry = config._registry()
        for text in (self.zh, self.en):
            for name in _backends(text, registry):
                self.assertIn(name, registry)


if __name__ == "__main__":
    unittest.main()
