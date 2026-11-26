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

from litcli import admission, project, scoring
from litcli.models import AcademicItem
from litcli.project import Project


def make_item(**kw) -> AcademicItem:
    base = dict(source="openalex", key="W1", title="Wick boiling", doi="10.1016/j.x.2020.1",
                year="2020", cited_by_count=10, extra={"score": 3.21, "evidence": 0.35})
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
        out = verdict(make_item(year="1999", extra={"evidence": 0.0, "seed": "search"}))
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

    def rows(self, *evidence, admit=True):
        return [{"identity": f"doi:{n}", "admit": admit, "reason": "evidence ok",
                 "evidence": e, "score": 0.0}
                for n, e in enumerate(evidence)]

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
        self.assertEqual(rows[0]["reason"], "evidence ok")

    def test_a_refused_row_stays_refused_when_there_is_room(self):
        rows = self.rows(0.3, 0.2)
        rows[1].update(admit=False, reason="evidence 0.10 < 0.20")
        admission.rank(rows, 5)
        self.assertFalse(rows[1]["admit"])
        self.assertEqual(rows[1]["reason"], "evidence 0.10 < 0.20")

    def test_rank_follows_evidence_before_the_score(self):
        # A famous recent work about something else can outscore an on-topic one on b/a/d; the
        # slot belongs to the evidence.
        rows = self.rows(0.1, 0.4)
        rows[0]["score"], rows[1]["score"] = 4.9, 1.1
        admission.rank(rows, 1)
        self.assertEqual([r["admit"] for r in rows], [False, True])

    def test_a_tie_keeps_the_order_it_arrived_in(self):
        rows = self.rows(3.0, 3.0)
        admission.rank(rows, 1)
        self.assertEqual([r["admit"] for r in rows], [True, False])


class FloorTest(unittest.TestCase):
    def test_a_low_evidence_is_refused_with_both_numbers(self):
        out = verdict(make_item(extra={"evidence": 0.05}))
        self.assertFalse(out["admit"])
        self.assertIn("0.05", out["reason"])
        self.assertIn(f"{project.DEFAULT_FLOOR:.2f}", out["reason"])

    def test_a_project_floor_overrides_the_default(self):
        out = verdict(make_item(extra={"evidence": 0.05}), make_project(floor=0.01))
        self.assertTrue(out["admit"])

    def test_an_admitted_evidence_states_the_comparison(self):
        out = verdict(make_item(extra={"evidence": 0.35}))
        self.assertTrue(out["admit"])
        self.assertEqual(out["reason"], f"evidence 0.35 >= {project.DEFAULT_FLOOR:.2f}")

    def test_a_high_total_without_evidence_is_refused(self):
        # 4.9 is a fresh, well-cited article of a research type: b+a+d alone reach 2.5.
        out = verdict(make_item(extra={"score": 4.9, "evidence": 0.0}))
        self.assertFalse(out["admit"])

    def test_the_floor_sits_above_one_word_and_below_two(self):
        # The whole point of the scale: one word of a long question is nearly nothing, two is a
        # signal. A floor that does not separate them either refuses everything at the first hop
        # or lets any paper through on a single generic word.
        question = "transient growth of viscoelastic instabilities in microfluidic flows"
        one = make_item(title="Growth over a plate", extra={"referenced_works": []})
        two = make_item(title="Growth instabilities in a microfluidic duct",
                        extra={"referenced_works": []})
        proj = make_project(question=question)
        floor = project.DEFAULT_FLOOR
        self.assertLess(scoring.evidence(one, proj, set()), floor)
        self.assertGreaterEqual(scoring.evidence(two, proj, set()), floor)

    def test_two_shared_references_are_enough(self):
        # Coupling is the same statement the cluster axis makes, in the share of the candidate's
        # own bibliography — the floor is two such references of a ten-reference list.
        item = make_item(extra={"referenced_works": ["W7", "W8"]})
        self.assertGreaterEqual(scoring.evidence(item, make_project(), {"W7", "W8"}),
                                project.DEFAULT_FLOOR)

    def test_one_shared_reference_alone_is_not_enough(self):
        # One of ten is the weakest signal there is; it should reach admission only alongside
        # something else, not on its own.
        item = make_item(extra={"referenced_works": ["W7"]})
        self.assertLess(scoring.evidence(item, make_project(), {"W7"}), project.DEFAULT_FLOOR)


class ReasonTest(unittest.TestCase):
    def test_every_verdict_has_a_non_empty_reason(self):
        cases = [make_item(), make_item(year="1999"),
                 make_item(extra={"evidence": 0.0})]
        for item in cases:
            self.assertTrue(verdict(item)["reason"].strip())
        self.assertTrue(verdict(make_item(), seen={"doi:10.1016/j.x.2020.1"})["reason"].strip())
