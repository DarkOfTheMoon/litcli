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

from litcli import project, scoring
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
    def test_ten_shared_references_saturate_the_axis(self):
        item = make_item(extra={"referenced_works": [f"W{i}" for i in range(10)]})
        parts = parts_of(item, make_project(), [f"W{i}" for i in range(10)])
        self.assertAlmostEqual(parts["c"], 3.0)

    def test_forty_shared_references_do_not_exceed_the_axis(self):
        item = make_item(extra={"referenced_works": [f"W{i}" for i in range(40)]})
        parts = parts_of(item, make_project(), [f"W{i}" for i in range(40)])
        self.assertAlmostEqual(parts["c"], 3.0)

    def test_no_shared_reference_is_zero(self):
        item = make_item(extra={"referenced_works": ["W9"]})
        self.assertAlmostEqual(parts_of(item, make_project(), ["W1"])["c"], 0.0)


class QuestionTest(unittest.TestCase):
    """The axis that knows the question: co-citation only says "like what we collected"."""

    def question_project(self):
        return make_project(question="reduced-order models for turbulent combustion")

    def test_a_title_answering_the_question_scores_above_one_that_does_not(self):
        on = make_item(title="Reduced-order models for turbulent combustion in a scramjet")
        off = make_item(title="A note on flame stretch")
        self.assertAlmostEqual(parts_of(on, self.question_project())["q"], scoring.W_QUESTION)
        self.assertAlmostEqual(parts_of(off, self.question_project())["q"], 0.0)

    def test_part_of_the_question_is_part_of_the_score(self):
        half = make_item(title="Reduced-order models for a reheat duct")
        whole = make_item(title="Reduced-order models for turbulent combustion")
        parts = parts_of(half, self.question_project())["q"]
        self.assertGreater(parts, 0.0)
        self.assertLess(parts, parts_of(whole, self.question_project())["q"])

    def test_a_title_hit_outweighs_an_abstract_hit(self):
        title = make_item(title="Turbulent combustion", abstract="")
        abstract = make_item(title="A note on flame stretch",
                             abstract="turbulent combustion in a dump combustor")
        self.assertGreater(parts_of(title, self.question_project())["q"],
                           parts_of(abstract, self.question_project())["q"])

    def test_an_off_topic_classic_loses_to_an_on_topic_paper(self):
        # The failure this axis exists for: a stall lets the classicness weight triple and the
        # walk drifts into the canon of whatever field the seeds came from.
        trace = [{"step": n, "new": 0} for n in range(1, 4)]
        classic = make_item(title="A note on flame stretch", year="2000", cited_by_count=20000)
        on_topic = make_item(title="Reduced-order models for turbulent combustion",
                             year="2018", cited_by_count=3)
        classic_total, _ = scoring.score(classic, self.question_project(), set(), list(trace))
        topic_total, _ = scoring.score(on_topic, self.question_project(), set(), list(trace))
        self.assertGreater(topic_total, classic_total)

    def test_half_the_question_scores_a_quarter_of_it(self):
        # Coverage is squared, so carrying half of what was asked is not half an answer: a
        # title with one word of a seven-word question would otherwise count as on-topic.
        question = "transient growth of viscoelastic instabilities in microfluidic flows"
        third = make_item(title="Growth instabilities in a duct")  # 2 of 6 content words
        self.assertAlmostEqual(parts_of(third, make_project(question=question))["q"],
                               scoring.W_QUESTION * (1 / 3) ** 2)

    def test_a_stopword_is_not_coverage(self):
        # "for" sits in nearly every title; counting it lets any paper answer any question.
        self.assertAlmostEqual(parts_of(make_item(title="A general approach"),
                                        make_project(question="models for flows"))["q"], 0.0)

    def test_a_different_word_form_still_counts(self):
        # Titles and questions disagree on number; "models" against "model" is the same word.
        self.assertAlmostEqual(parts_of(make_item(title="A model for flow"),
                                        make_project(question="models for flows"))["q"],
                               scoring.W_QUESTION)

    def test_a_one_word_question_cannot_be_answered_by_one_hit(self):
        self.assertAlmostEqual(parts_of(make_item(title="Boiling"),
                                        make_project(question="boiling"))["q"],
                               scoring.W_QUESTION * 0.25)

    def test_a_generic_review_matching_one_word_of_seven_is_no_evidence(self):
        # Three thousand citations and a place in the canon do not make it about the question:
        # one word of six is under any sane floor, and the total cannot rescue it.
        question = "transient growth of viscoelastic instabilities in microfluidic flows"
        review = make_item(title="The origins and the future of microfluidics",
                           year="2003", cited_by_count=3000)
        proj = make_project(question=question)
        self.assertLess(scoring.evidence(review, proj, set()), project.DEFAULT_FLOOR)

    def test_the_cluster_axis_still_outweighs_the_question_axis(self):
        # Co-citation is the better evidence once the collection is on topic; the question is
        # what keeps it there.
        self.assertGreater(scoring.W_CLUSTER, scoring.W_QUESTION)

    def test_a_question_of_punctuation_alone_scores_zero(self):
        self.assertAlmostEqual(parts_of(make_item(), make_project(question="--"))["q"], 0.0)


class EvidenceTest(unittest.TestCase):
    """What the candidate itself carries — the only thing admission may look at.

    Recency, classicness and type say when and how famous a work is, never what it is about: a
    fresh, well-cited article about something else scores on all three and would otherwise walk
    through any gate built on the total.
    """

    def project(self, question="reduced-order models for turbulent combustion"):
        return make_project(question=question)

    def test_a_shared_reference_is_evidence(self):
        item = make_item(extra={"referenced_works": ["W7"]})
        self.assertAlmostEqual(scoring.evidence(item, self.project(), {"W7"}), 0.1)

    def test_ten_shared_references_saturate(self):
        refs = [f"W{i}" for i in range(10)]
        item = make_item(extra={"referenced_works": refs})
        self.assertAlmostEqual(scoring.evidence(item, self.project(), set(refs)), 1.0)

    def test_question_words_are_evidence(self):
        partial = make_item(title="Reduced-order models for a turbulent wake",
                            extra={"referenced_works": []})
        self.assertGreater(scoring.evidence(partial, self.project(), set()), 0.0)

    def test_one_half_of_a_two_part_question_is_no_evidence(self):
        # "X for Y" is a conjunction: work on reduced-order models of something else is not
        # work on reduced-order models of combustion.
        half = make_item(title="Reduced-order models for a reheat duct",
                         extra={"referenced_works": []})
        self.assertAlmostEqual(scoring.evidence(half, self.project(), set()), 0.0)

    def test_nothing_carried_is_zero(self):
        item = make_item(title="A note on flame stretch", extra={"referenced_works": ["W9"]})
        self.assertAlmostEqual(scoring.evidence(item, self.project(), {"W1"}), 0.0)

    def test_coupling_is_shared_over_the_candidates_own_bibliography(self):
        # Ten shared references are a tenth of a 100-reference list but half of a focused
        # 20-reference one. Unnormalized, any long review saturates the axis on any topic: a
        # 640-reference generic review and a 43-reference on-topic paper both hit the cap.
        shared = {f"W{i}" for i in range(10)}
        review = make_item(extra={"referenced_works": [f"W{i}" for i in range(400)]})
        focused = make_item(extra={"referenced_works": [f"W{i}" for i in range(20)]})
        self.assertAlmostEqual(scoring.evidence(focused, self.project(), shared), 0.5)
        self.assertAlmostEqual(scoring.evidence(review, self.project(), shared), 0.025)
        self.assertLess(scoring.evidence(review, self.project(), shared),
                        project.DEFAULT_FLOOR)

    def test_a_citation_from_inside_the_collection_is_evidence(self):
        # A direct citation is the strongest local signal there is — better than a shared
        # reference (coupling), which only says two works read the same papers.
        item = make_item(key="openalex:W5", title="A note on flame stretch",
                         extra={"referenced_works": ["W9"]})
        self.assertAlmostEqual(scoring.evidence(item, self.project(), {"W1"}), 0.0)
        cited = {"W5": 1}
        self.assertGreaterEqual(scoring.evidence(item, self.project(), {"W1"}, cited),
                                project.DEFAULT_FLOOR)

    def test_three_citations_from_inside_the_collection_saturate(self):
        item = make_item(key="W5", title="A note", extra={"referenced_works": []})
        self.assertAlmostEqual(scoring.evidence(item, self.project(), set(), {"W5": 3}), 1.0)

    def test_a_reference_and_a_word_add_up_but_stay_capped(self):
        refs = [f"W{i}" for i in range(10)]
        item = make_item(title="Reduced-order models for turbulent combustion",
                         extra={"referenced_works": refs})
        self.assertAlmostEqual(scoring.evidence(item, self.project(), set(refs)), 1.0)


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

    def test_a_seed_and_a_reference_match_across_their_url_forms(self):
        # The seed is written as plain ids, the graph hands out full openalex URLs.
        project = make_project(seeds=["openalex:W22"], year_from="", year_to="")
        item = make_item(year="2020",
                         extra={"referenced_works": ["https://openalex.org/W22"]})
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

    def test_a_trace_shaped_history_counts_the_hops_in_it(self):
        # The loop hands over the whole event trace, not one record per hop: only `hop_end`
        # carries `new`. Counting a hop's three candidates as three dry hops would flip the
        # weight right after a hop that found plenty.
        trace = [{"event": "hop_end", "new": 5}, {"event": "candidate"}, {"event": "admit"},
                 {"event": "download"}]
        self.assertFalse(scoring.sparse(make_project(), trace))

    def test_a_trace_shaped_history_still_sees_a_dry_run(self):
        trace = [{"event": "hop_end", "new": 0}, {"event": "candidate"},
                 {"event": "hop_end", "new": 1}, {"event": "candidate"},
                 {"event": "hop_end", "new": 0}, {"event": "candidate"}]
        self.assertTrue(scoring.sparse(make_project(), trace))


class TotalTest(unittest.TestCase):
    def test_the_total_is_the_sum_of_the_parts(self):
        item = make_item(extra={"openalex_type": "review", "referenced_works": ["W7"]})
        total, parts = scoring.score(item, make_project(), {"W7"}, [])
        self.assertAlmostEqual(total, sum(parts.values()), places=5)
