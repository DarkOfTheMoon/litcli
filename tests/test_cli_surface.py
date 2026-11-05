"""CLI surface smoke tests: every parser path answers ``--help`` and every leaf is dispatched.

The argparse surface is the part most easily broken by a refactor (renamed subcommand, dropped
option, a command added but never wired into ``cli``) and it is not exercised by the backend
stubs. These tests walk the parser itself and touch neither the network nor the local library.
"""
import argparse
import contextlib
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from litcli import cli
from litcli import completions as completions_mod
from litcli.commands import graph
from litcli.commands.parser import build_parser

#: The surface the docs, the skill and the completion scripts all promise.
EXPECTED = {
    "search", "refs", "citing", "related", "export", "completions", "backends", "data", "fetch",
}
EXPECTED_PARENTS = {
    "data": {"get", "add", "set", "del", "attach", "tree", "members"},
    "backends": {"list", "status", "authorize"},
}


def _subparsers(parser):
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices
    return {}


def _leaves(parser, path=()):
    """Yield every command path, including intermediates (``data`` as well as ``data get``)."""
    yield path
    for name, child in _subparsers(parser).items():
        yield from _leaves(child, (*path, name))


class CliSurfaceTest(unittest.TestCase):
    def setUp(self):
        self.parser = build_parser()

    def test_expected_commands_exist(self):
        self.assertEqual(set(_subparsers(self.parser)), EXPECTED)
        for parent, children in EXPECTED_PARENTS.items():
            self.assertEqual(set(_subparsers(_subparsers(self.parser)[parent])), children, parent)

    def test_every_command_answers_help(self):
        for path in _leaves(self.parser):
            with self.subTest(path=" ".join(path) or "<root>"):
                out = io.StringIO()
                with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as ctx:
                    self.parser.parse_args([*path, "--help"])
                self.assertEqual(ctx.exception.code, 0)
                self.assertIn("usage:", out.getvalue())

    def test_every_command_is_dispatched(self):
        """A parser entry with no handler fails nothing until someone runs it — pin the wiring."""
        dispatched = set(cli._TOP_LEVEL_COMMANDS) | set(graph.EDGE_METHODS)
        dispatched |= set(cli._DATA_COMMANDS)
        dispatched |= {"data", "fetch", "backends"}
        self.assertEqual(set(_subparsers(self.parser)) - dispatched, set())
        self.assertEqual(set(cli._DATA_COMMANDS), EXPECTED_PARENTS["data"])

    def test_completion_spec_covers_the_surface(self):
        spec = completions_mod.build_spec(self.parser)
        self.assertEqual(set(spec["top"]), EXPECTED)

    def test_version_flag(self):
        with self.assertRaises(SystemExit) as ctx, contextlib.redirect_stdout(io.StringIO()) as out:
            self.parser.parse_args(["--version"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn("litcli ", out.getvalue())


if __name__ == "__main__":
    unittest.main()
