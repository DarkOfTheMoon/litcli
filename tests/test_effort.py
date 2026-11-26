"""The effort levels are envelopes, not budgets.

How much material a survey gets is a property of the topic, so the user picks a level and each
envelope says how wide every site may be walked. The four levels are a table; a project may
override single cells.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from litcli import effort
from litcli.errors import BadInput

#: level -> the whole row, in the interface's key order.
TABLE = {
    "low": dict(frontier=4, admit=2, refs=10, citing=10, related=5, streak=2,
                graph_cap=120, arxiv_cap=10, publisher_cap=2, max_hops=6),
    "medium": dict(frontier=8, admit=5, refs=20, citing=20, related=10, streak=3,
                   graph_cap=1000, arxiv_cap=40, publisher_cap=6, max_hops=20),
    "high": dict(frontier=16, admit=8, refs=30, citing=30, related=15, streak=4,
                 graph_cap=4000, arxiv_cap=100, publisher_cap=15, max_hops=50),
    "max": dict(frontier=24, admit=12, refs=50, citing=50, related=25, streak=6,
                graph_cap=14400, arxiv_cap=200, publisher_cap=30, max_hops=120),
}


class TableTest(unittest.TestCase):
    def test_the_levels_are_the_four_named_ones(self):
        self.assertEqual(effort.LEVELS, ("low", "medium", "high", "max"))
        self.assertEqual(effort.DEFAULT, "medium")

    def test_every_cell_matches_the_table(self):
        for level, row in TABLE.items():
            with self.subTest(level=level):
                self.assertEqual(effort.resolve(level), row)

    def test_the_site_allowance_covers_the_walk_the_level_allows(self):
        # Measured on a live `low` walk (4 hops, 2 rows each, openalex): a row's expansion costs
        # ~10 requests, because `references` is one call plus one per cited work. A site budget
        # below `admit × max_hops × 10` is spent by the second hop, so the walk stops on
        # "budget" long before the size the level itself names.
        for level in effort.LEVELS:
            with self.subTest(level=level):
                row = effort.resolve(level)
                self.assertGreaterEqual(row["graph_cap"], row["admit"] * row["max_hops"] * 10)

    def test_resolve_hands_out_a_copy(self):
        first = effort.resolve("low")
        first["admit"] = 99
        self.assertEqual(effort.resolve("low")["admit"], 2)


class OverrideTest(unittest.TestCase):
    def test_one_override_leaves_the_rest_of_the_row_alone(self):
        row = effort.resolve("low", {"admit": 9})
        self.assertEqual(row["admit"], 9)
        self.assertEqual(row["frontier"], 4)


class UnknownLevelTest(unittest.TestCase):
    def test_an_unknown_level_is_a_bad_input(self):
        with self.assertRaises(BadInput):
            effort.resolve("insane")

    def test_an_empty_level_is_a_bad_input(self):
        with self.assertRaises(BadInput):
            effort.resolve("")
