"""Result ordering and filter enforcement inside ``search``.

Backend APIs are not a guarantee: a filter one source ignores must not leak records, and the
rows under ``--limit`` must be the ones that answer the query rather than whoever answered first.
"""
import argparse
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from litcli.commands import search
from litcli.commands.search import matches, relevance
from litcli.models import AcademicItem


def _item(title="T", **kw):
    base = dict(source="openalex", key=kw.pop("key", "10.1/x"), title=title, year="2020")
    base.update(kw)
    return AcademicItem(**base)


class MatchesTest(unittest.TestCase):
    def test_title_is_a_substring_match_either_case(self):
        it = _item("Graph Neural Networks")
        self.assertTrue(matches(it, {"title": "neural"}))
        self.assertFalse(matches(it, {"title": "transformer"}))

    def test_author_matches_given_or_family(self):
        it = _item(creators=[{"family": "LeCun", "given": "Yann"}])
        self.assertTrue(matches(it, {"author": "lecun"}))
        self.assertTrue(matches(it, {"author": "yann"}))
        self.assertFalse(matches(it, {"author": "hinton"}))

    def test_year_is_a_prefix_match(self):
        it = _item(year="2021-05")
        self.assertTrue(matches(it, {"year": "2021"}))
        self.assertFalse(matches(it, {"year": "2019"}))

    def test_container_and_no_filter(self):
        self.assertTrue(matches(_item(container_title="Nature"), {"container": "nature"}))
        self.assertTrue(matches(_item(), {}))


class RelevanceTest(unittest.TestCase):
    def test_a_title_hit_beats_an_abstract_hit(self):
        title = _item("Graph neural networks")
        abstract = _item("Something else", abstract="graph neural networks at scale")
        self.assertLess(relevance(title, "graph neural networks"),
                        relevance(abstract, "graph neural networks"))

    def test_all_tokens_in_the_title_wins(self):
        exact = _item("Graph neural networks for CFD")
        partial = _item("Neural networks", abstract="graph")
        self.assertLess(relevance(exact, "graph neural networks"), relevance(partial, "graph neural networks"))

    def test_a_stopword_hit_does_not_stand_for_the_query(self):
        # "for" is in nearly every title. Counted, it ties a title that carries a real query word
        # and the tie falls through to the title's alphabetical order ("A tool…" first).
        stopword_only = _item("A tool for analysts")
        on_topic = _item("Flow regimes")
        self.assertLess(relevance(on_topic, "flow for analysis"),
                        relevance(stopword_only, "flow for analysis"))

    def test_the_query_words_drop_stopwords(self):
        self.assertEqual(search._tokens("for the flows of"), ["flows"])

    def test_a_question_is_split_into_its_parts(self):
        self.assertEqual(search.legs("machine learning for nuclear thermal hydraulics"),
                         [["machine", "learning"], ["nuclear", "thermal", "hydraulics"]])

    def test_a_question_without_a_connective_is_one_part(self):
        self.assertEqual(search.legs("wick boiling"), [["wick", "boiling"]])

    def test_two_parts_both_have_to_be_there(self):
        # "X for Y" is a conjunction: the half that answers only one of the two is not the
        # thing that was asked for.
        both = _item("Machine learning for nuclear thermal hydraulics")
        half = _item("Nuclear thermal hydraulics of a water loop")
        q = "machine learning for nuclear thermal hydraulics"
        self.assertGreater(search.overlap(both, q), search.overlap(half, q))
        self.assertAlmostEqual(search.overlap(half, q), 0.0)

    def test_one_of_three_parts_may_be_missing(self):
        # The literature answers a three-part question two parts at a time and almost never all
        # three in one title; demanding all three starves the walk.
        q = "transient growth of viscoelastic instabilities in microfluidic flows"
        two_of_three = _item("Viscoelastic instabilities in microfluidic flows")
        one_of_three = _item("Droplet microfluidic platforms")
        self.assertAlmostEqual(search.overlap(two_of_three, q), 1.0)
        self.assertAlmostEqual(search.overlap(one_of_three, q), 0.0)

    def test_a_long_question_may_lose_a_quarter_of_its_parts(self):
        # Four parts or more: a quarter may be missing, rounded down, never fewer than one part.
        q = "transient growth of viscoelastic instabilities in narrow microfluidic channel flows"
        most = _item("Transient growth of viscoelastic instabilities in microfluidic flows")
        self.assertGreater(search.overlap(most, q), 0.0)

    def test_a_plural_and_its_singular_are_the_same_word(self):
        # Titles and questions disagree on number constantly ("instabilities" vs "instability",
        # "flows" vs "flow"); a rule that only compares literal spellings loses the on-topic
        # half of every question that happens to be plural.
        singular = search.overlap(_item("Instability of channel flow"),
                                  "instability of channel flow")
        self.assertGreater(singular, 0.0)
        self.assertAlmostEqual(
            search.overlap(_item("Instability of channel flow"),
                           "instabilities of channel flows"), singular)

    def test_an_acronym_carries_the_method_it_stands_for(self):
        # The literature writes the same method three ways ("reduced-order models", "low-order
        # models", "ROM", "PCA", "LES"); a question spelled one way loses every title spelled
        # another. The acronym stands for the whole leg, so it matches its words.
        self.assertTrue(search._same("pca", "reduced"))
        self.assertTrue(search._same("les", "order"))
        self.assertTrue(search._same("manifold", "models"))

    def test_an_acronym_alone_carries_the_leg(self):
        # No "models" in the title: the acronym has to hold the part up by itself, which is
        # what the group member buys over the shared "model" word.
        q = "reduced-order models for turbulent combustion"
        self.assertGreater(search.overlap(
            _item("Principal component analysis of turbulent combustion"), q), 0.0)

    def test_a_low_order_model_answers_a_reduced_order_question(self):
        q = "reduced-order models for turbulent combustion"
        self.assertGreater(search.overlap(
            _item("Low-order modeling of combustion instability"), q), 0.2)

    def test_a_method_word_is_not_a_synonym_of_everything(self):
        # The groups are per-topic, not a thesaurus: "large" only stands for "LES" when the
        # question says so, and combustion words do not answer a ROM leg.
        self.assertFalse(search._same("large", "les"))
        self.assertFalse(search._same("combustion", "manifold"))

    def test_a_compound_word_carries_its_part(self):
        self.assertGreater(search.overlap(_item("Purely elastic instability"),
                                          "viscoelastic instabilities"), 0.0)

    def test_a_combustor_carries_combustion(self):
        # The fixture labels the ROM question's "Model Combustor" row off, and it stays off on
        # the second part alone; here the point is only that the noun form is the same topic.
        self.assertTrue(search._same("combustor", "combustion"))
        self.assertGreater(search.overlap(_item("Combustion instability in a model combustor"),
                                          "turbulent combustion"), 0.0)

    def test_a_temperature_is_thermal(self):
        self.assertTrue(search._same("temperature", "thermal"))

    def test_a_microchannel_is_a_microfluidic_flow(self):
        # The transient question says "microfluidic flows"; a paper working in a microchannel
        # is the same setting spelled from the geometry side.
        self.assertTrue(search._same("microchannel", "microfluidic"))

    def test_citations_break_a_tie(self):
        weak = _item("Graph analysis")
        strong = _item("Graph analysis", cited_by_count=90)
        self.assertLess(relevance(strong, "graph"), relevance(weak, "graph"))


class SearchOrderTest(unittest.TestCase):
    def _run(self, items, argv):
        args = argparse.Namespace(backend="openalex", query="graph neural networks", limit=2,
                                 format="lines", no_cite=True, type=None, sort=None,
                                 title=None, author=None, year=None, container=None)
        for k, v in argv.items():
            setattr(args, k, v)
        seen = []
        backend = mock.Mock()
        backend.name = "openalex"
        backend.search.return_value = items
        with mock.patch.object(search, "get_backend", return_value=backend), \
                mock.patch.object(search, "print_items", side_effect=lambda its, *a, **k: seen.extend(its)):
            search.cmd_search({}, args)
        return seen

    def test_the_limit_keeps_the_relevant_rows(self):
        off, on = _item("Off topic", abstract="graph"), _item("Graph neural networks")
        seen = self._run([off, on], {"limit": 1})
        self.assertEqual([it.title for it in seen], ["Graph neural networks"])

    def test_a_filter_the_backend_ignored_is_still_enforced(self):
        seen = self._run([_item("Graph neural networks"), _item("Graph databases")],
                         {"title": "neural"})
        self.assertEqual([it.title for it in seen], ["Graph neural networks"])

    def test_sort_cited_is_not_reordered_by_relevance(self):
        old = _item("Something else", cited_by_count=500)
        new = _item("Graph neural networks")
        seen = self._run([new, old], {"sort": "cited", "limit": 2})
        self.assertEqual([it.cited_by_count for it in seen], [500, 0])


if __name__ == "__main__":
    unittest.main()
