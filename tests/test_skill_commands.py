"""Keep ``skills/litcli/SKILL.md`` in step with the real CLI surface.

The skill is the single source agents load, so a stale command table silently teaches them
commands that no longer exist (or hides ones that do). AGENTS.md makes the sync a convention;
these tests make it a gate.
"""
import argparse
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from litcli import config
from litcli.commands.parser import build_parser

SKILL = os.path.join(os.path.dirname(__file__), "..", "skills", "litcli", "SKILL.md")

#: ``litcli <cmd> [<sub>]`` inside a code span — prose like "litcli 与用户的资料互操作" is skipped.
_COMMAND_RE = re.compile(r"`litcli\s+([a-z][a-z-]*)(?:\s+([a-z][a-z-]*))?")
#: ``--backend <name>`` (possibly an escaped alternation, ``a\|b``) inside a code span.
_BACKEND_RE = re.compile(r"`litcli[^`]*--backend\s+([a-z\\|]+)")


def _backends(text):
    return {name for group in _BACKEND_RE.findall(text) for name in group.split("\\|") if name}


def _subparsers(parser):
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices
    return {}


class SkillCommandTableTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tree = _subparsers(build_parser())
        with open(SKILL, encoding="utf-8") as f:
            cls.text = f.read()
        cls.mentioned = _COMMAND_RE.findall(cls.text)

    def test_every_command_is_documented(self):
        mentioned = {cmd for cmd, _ in self.mentioned}
        self.assertEqual(set(self.tree) - mentioned, set(), "commands missing from SKILL.md")

    def test_no_command_is_invented(self):
        for cmd, sub in self.mentioned:
            self.assertIn(cmd, self.tree, f"SKILL.md documents `litcli {cmd}`, which is not a command")
            children = _subparsers(self.tree[cmd])
            if sub and children:
                self.assertIn(sub, children, f"SKILL.md documents `litcli {cmd} {sub}`")

    def test_documented_backends_exist(self):
        registry = config._registry()
        for name in _backends(self.text):
            self.assertIn(name, registry, f"SKILL.md names `--backend {name}`, which is not a backend")


if __name__ == "__main__":
    unittest.main()
