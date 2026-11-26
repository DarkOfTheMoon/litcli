"""Admission: the checks a candidate runs before it may enter the collection.

Every verdict is a record with a reason, refusals included — a survey that silently drops
candidates cannot be audited, and the user's question at the end is always "why is this here
and why is that not". Seeds are the exception the spec names: they were chosen by the user (or
by the opening search), so they bypass the year filter and the threshold alike.
"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from litcli import admission
from litcli.models import AcademicItem
from litcli.project import Project


def make_item(**kw) -> AcademicItem:
    base = dict(source="openalex", key="W1", title="Wick boiling", doi="10.1016/j.x.2020.1",
                year="2020", cited_by_count=10, extra={"score": 3.21})
    base.update(kw)
    return AcademicItem(**base)


def make_project(**kw) -> Project:
    base = dict(root=Path("/nonexistent"), question="q", year_from="2015", year_to="2025")
    base.update(kw)
    return Project(**base)


def verdict(item, project=None, seen=()):
    return admission.decide(item, project or make_project(), set(seen))


class IdentityTest(unittest.TestCase):
    def test_the_verdict_carries_the_identity_it_judged(self):
        self.assertEqual(verdict(make_item())["identity"], "doi:10.1016/j.x.2020.1")

    def test_the_identity_is_the_normalized_doi(self):
        item = make_item(doi="https://doi.org/10.1016/J.X.2020.1")
        self.assertEqual(verdict(item)["identity"], "doi:10.1016/j.x.2020.1")


class SeenTest(unittest.TestCase):
    def test_a_seen_identity_is_refused_with_its_reason(self):
        out = verdict(make_item(), seen={"doi:10.1016/j.x.2020.1"})
        self.assertFalse(out["admit"])
        self.assertIn("seen", out["reason"])


class YearTest(unittest.TestCase):
    def test_a_year_outside_the_window_is_refused(self):
        out = verdict(make_item(year="1999"))
        self.assertFalse(out["admit"])
        self.assertIn("1999", out["reason"])

    def test_a_seed_is_exempt_from_the_year_filter(self):
        out = verdict(make_item(year="1999", extra={"score": 0.1, "seed": "search"}))
        self.assertTrue(out["admit"])
        self.assertEqual(out["reason"], "seed")


class ContainerTest(unittest.TestCase):
    def test_a_container_mismatch_is_refused(self):
        out = verdict(make_item(container_title="Journal of Fluids"),
                      make_project(container="Heat Transfer"))
        self.assertFalse(out["admit"])
        self.assertIn("Heat Transfer", out["reason"])

    def test_a_container_hit_passes(self):
        out = verdict(make_item(container_title="International Journal of Heat Transfer"),
                      make_project(container="heat transfer"))
        self.assertTrue(out["admit"])


class RankTest(unittest.TestCase):
    """How many may enter is the envelope's call; which of them is the score's."""

    def rows(self, *scores, admit=True):
        return [{"identity": f"doi:{n}", "admit": admit, "reason": "score ok", "score": s}
                for n, s in enumerate(scores)]

    def test_only_the_best_are_kept(self):
        rows = self.rows(2.0, 3.5, 2.8)
        admission.rank(rows, 2)
        self.assertEqual([r["admit"] for r in rows], [False, True, True])

    def test_a_demoted_row_says_where_it_placed(self):
        rows = self.rows(2.0, 3.5, 2.8)
        admission.rank(rows, 2)
        self.assertIn("rank 3 of 3", rows[0]["reason"])

    def test_room_to_spare_demotes_nobody(self):
        rows = self.rows(2.0, 3.5)
        admission.rank(rows, 2)
        self.assertTrue(all(r["admit"] for r in rows))
        self.assertEqual(rows[0]["reason"], "score ok")

    def test_a_refused_row_stays_refused_when_there_is_room(self):
        rows = self.rows(3.0, 2.0)
        rows[1].update(admit=False, reason="score 1.00 < 2.50")
        admission.rank(rows, 5)
        self.assertFalse(rows[1]["admit"])
        self.assertEqual(rows[1]["reason"], "score 1.00 < 2.50")

    def test_a_tie_keeps_the_order_it_arrived_in(self):
        rows = self.rows(3.0, 3.0)
        admission.rank(rows, 1)
        self.assertEqual([r["admit"] for r in rows], [True, False])


class ThresholdTest(unittest.TestCase):
    def test_a_low_score_is_refused_with_both_numbers(self):
        out = verdict(make_item(extra={"score": 1.5}))
        self.assertFalse(out["admit"])
        self.assertIn("1.50", out["reason"])
        self.assertIn("2.50", out["reason"])

    def test_a_project_threshold_overrides_the_default(self):
        out = verdict(make_item(extra={"score": 1.5}), make_project(threshold=1.0))
        self.assertTrue(out["admit"])

    def test_an_admitted_score_states_the_comparison(self):
        out = verdict(make_item(extra={"score": 3.21}))
        self.assertTrue(out["admit"])
        self.assertEqual(out["reason"], "score 3.21 >= 2.50")


class ReasonTest(unittest.TestCase):
    def test_every_verdict_has_a_non_empty_reason(self):
        cases = [make_item(), make_item(year="1999"),
                 make_item(extra={"score": 0.0})]
        for item in cases:
            self.assertTrue(verdict(item)["reason"].strip())
        self.assertTrue(verdict(make_item(), seen={"doi:10.1016/j.x.2020.1"})["reason"].strip())
