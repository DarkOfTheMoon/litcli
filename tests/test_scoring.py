"""Composite scoring: cluster skeleton first, recency second, classicness as the fallback.

The three axes are normalized to 0..1 before any weight is applied, so a weight change moves
one axis and nothing else. The weights are ``c=3.0``, ``b=1.0`` and ``a=0.5`` — ``a`` climbs to
1.5 while the cluster is sparse, which is the whole point of a fallback: when the skeleton
says nothing, classicness speaks up. Article type is the fourth, declared axis.
"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from litcli import scoring
from litcli.models import AcademicItem
from litcli.project import Project


def make_item(**kw) -> AcademicItem:
    base = dict(source="openalex", key="W1", title="A paper", year="2020", cited_by_count=20,
                extra={"openalex_type": "article", "referenced_works": []})
    base.update(kw)
    return AcademicItem(**base)


def make_project(**kw) -> Project:
    base = dict(root=Path("/nonexistent"), question="q", year_from="2015", year_to="2025")
    base.update(kw)
    return Project(**base)


def parts_of(item, project, collected_refs=(), trace=()):
    _total, parts = scoring.score(item, project, set(collected_refs), list(trace))
    return parts


class ClusterTest(unittest.TestCase):
    def test_twenty_shared_references_saturate_the_axis(self):
        item = make_item(extra={"referenced_works": [f"W{i}" for i in range(20)]})
        parts = parts_of(item, make_project(), [f"W{i}" for i in range(20)])
        self.assertAlmostEqual(parts["c"], 3.0)

    def test_forty_shared_references_do_not_exceed_the_axis(self):
        item = make_item(extra={"referenced_works": [f"W{i}" for i in range(40)]})
        parts = parts_of(item, make_project(), [f"W{i}" for i in range(40)])
        self.assertAlmostEqual(parts["c"], 3.0)

    def test_no_shared_reference_is_zero(self):
        item = make_item(extra={"referenced_works": ["W9"]})
        self.assertAlmostEqual(parts_of(item, make_project(), ["W1"])["c"], 0.0)


class RecencyTest(unittest.TestCase):
    def test_the_window_middle_sits_halfway(self):
        self.assertAlmostEqual(parts_of(make_item(year="2020"), make_project())["b"], 0.5)

    def test_the_window_end_saturates(self):
        self.assertAlmostEqual(parts_of(make_item(year="2025"), make_project())["b"], 1.0)

    def test_an_out_of_window_year_is_zero(self):
        self.assertAlmostEqual(parts_of(make_item(year="1999"), make_project())["b"], 0.0)

    def test_citing_a_seed_adds_half(self):
        project = make_project(seeds=["W22"], year_from="", year_to="")  # no window: base 0.5
        item = make_item(year="2020", extra={"referenced_works": ["W22"]})
        self.assertAlmostEqual(parts_of(item, project)["b"], 1.0)


class ClassicnessTest(unittest.TestCase):
    def test_citations_per_year_is_the_axis(self):
        # 120 citations at age 6 == 20 per year -> saturated.
        self.assertAlmostEqual(parts_of(make_item(cited_by_count=120), make_project())["a"], 0.5)

    def test_a_sparse_cluster_raises_the_weight(self):
        trace = [{"step": 1, "new": 0}, {"step": 2, "new": 1}, {"step": 3, "new": 1}]
        self.assertAlmostEqual(parts_of(make_item(cited_by_count=120), make_project(),
                                        trace=trace)["a"], 1.5)

    def test_an_unknown_year_does_not_divide_by_zero(self):
        parts = parts_of(make_item(year="", cited_by_count=5), make_project())
        self.assertGreaterEqual(parts["a"], 0.0)


class TypeTest(unittest.TestCase):
    def test_a_declared_type_carries_twice_an_undeclared_one(self):
        item = make_item(extra={"openalex_type": "review"})
        plain, declared = make_project(), make_project(type_weight={"review": 2.0})
        excluded = make_project(type_weight={"review": 0.0})
        t_none, _ = scoring.score(item, plain, set(), [])
        t_declared, _ = scoring.score(item, declared, set(), [])
        t_excluded, _ = scoring.score(item, excluded, set(), [])
        self.assertAlmostEqual(t_declared - t_excluded, 2 * (t_none - t_excluded))

    def test_an_editorial_weighs_less_than_an_article(self):
        article = parts_of(make_item(extra={"openalex_type": "article"}), make_project())
        editorial = parts_of(make_item(extra={"openalex_type": "editorial"}), make_project())
        self.assertGreater(article["d"], editorial["d"])


class SparseTest(unittest.TestCase):
    def test_few_new_candidates_over_three_hops_is_sparse(self):
        trace = [{"step": n, "new": 0} for n in range(1, 4)]
        self.assertTrue(scoring.sparse(make_project(), trace))

    def test_a_short_history_is_not_sparse_yet(self):
        self.assertFalse(scoring.sparse(make_project(), [{"step": 1, "new": 0}]))

    def test_a_productive_streak_is_not_sparse(self):
        trace = [{"step": n, "new": 3} for n in range(1, 5)]
        self.assertFalse(scoring.sparse(make_project(), trace))


class TotalTest(unittest.TestCase):
    def test_the_total_is_the_sum_of_the_parts(self):
        item = make_item(extra={"openalex_type": "review", "referenced_works": ["W7"]})
        total, parts = scoring.score(item, make_project(), {"W7"}, [])
        self.assertAlmostEqual(total, sum(parts.values()), places=5)
