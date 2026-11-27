"""A replay of three walks, row by row, with the labels read by a human.

The labels are what a reviewer of the titles says the question is about and is not — not the
verdict the walk returned. That matters for the rows the walk got wrong in both directions: a
row refused although it is on the question is labelled ``on``, and one admitted although it is
off it is labelled ``off``, so the fixture carries the truth rather than a copy of the code.

What the replay re-derives is the question-side term (:func:`litcli.commands.search.overlap`);
the cluster and citer terms are frozen at whatever makes the recorded evidence (so ``coupling``
and ``citers`` are residual, not the raw counts), because reproducing them would need the
collection as it stood at every hop. So this pins "does the question still admit and refuse
what it did", not the whole arithmetic.

It is a snapshot of one moment (the 2026-11-26 ranking work, 0.2 floor, three walks). It guards
against a change losing a row that was rightly kept or admitting one that was rightly dropped;
it does not prove one ranking better than another, and a legitimate improvement is expected to
move these numbers and to be re-labelled with them.

Rows labelled ``known_miss`` are on the question and still fall under the floor, and rows
labelled ``known_admit`` are off it and still cleared the floor: the fixture records both gaps
instead of hiding them, and flags a row as stale the day a change closes it.
"""
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from litcli import project
from litcli.commands.search import overlap
from litcli.models import AcademicItem

FIXTURES = Path(__file__).parent / "fixtures" / "ranking"
#: How far the question-side arithmetic may drift before the labels need re-reading.
DRIFT = 0.05


class RankingSnapshotTest(unittest.TestCase):
    def test_the_snapshot_is_not_empty_and_pinned_to_the_default_floor(self):
        # The per-fixture loop proves nothing when the directory is gone, and a fixture floor
        # that drifted from DEFAULT_FLOOR would label every row against the wrong line.
        paths = sorted(FIXTURES.glob("*.json"))
        self.assertEqual(len(paths), 3)
        for path in paths:
            with open(path, encoding="utf-8") as fh:
                fixture = json.load(fh)
            self.assertEqual(fixture["floor"], project.DEFAULT_FLOOR)
            # `walk` says which live run a fixture was read from: without it a fixture cannot
            # be traced back, and a typo'd label would go unnoticed.
            self.assertTrue(str(fixture.get("walk") or "").strip(), "a fixture must name its walk")

    def test_every_fixture_keeps_its_verdicts(self):
        for path in sorted(FIXTURES.glob("*.json")):
            with self.subTest(walk=path.stem):
                with open(path, encoding="utf-8") as fh:
                    fixture = json.load(fh)
                self.assertTrue(fixture["rows"], "an empty fixture proves nothing")
                for row in fixture["rows"]:
                    with self.subTest(title=row["title"]):
                        title = row["title"]
                        carried = overlap(AcademicItem(source="", key=title, title=title),
                                          fixture["question"])
                        self.assertAlmostEqual(carried, row["overlap"], delta=DRIFT,
                                               msg="the question-side arithmetic moved")
                        evidence = min(1.0, carried + row["coupling"] + row["citers"])
                        under = evidence < fixture["floor"]
                        if row["label"] == "on" and not row.get("known_miss"):
                            self.assertFalse(under, f"rightly kept, now refused: {title}")
                        elif row["label"] == "on":
                            self.assertTrue(under, "the known miss is closed: drop the flag "
                                                   f"and re-read the label ({title})")
                        elif row.get("known_admit"):
                            self.assertFalse(under, "the known admission is closed: drop the "
                                                    f"flag and re-read the label ({title})")
                        else:
                            self.assertTrue(under, f"rightly refused, now admitted: {title}")


if __name__ == "__main__":
    unittest.main()
