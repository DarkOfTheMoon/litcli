"""`litcli survey new`: the question, its filters and its seeds, written to a project.

``survey new`` is the only place a survey's constraints are set, so it is where they must land
in the file verbatim. Without ``--seed`` it runs one opening search and takes the first
candidates as the initial cluster; an opening search that finds nothing is an error, not an
empty survey.
"""
import argparse
import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from litcli import project
from litcli.commands import survey
from litcli.errors import BotWall, NotFound
from litcli.models import AcademicItem


class FakeBackend:
    name = "openalex"

    def __init__(self):
        self.search_calls = 0
        self.hits = [AcademicItem(source="openalex", key=f"W{i}", title=f"hit {i}",
                                  doi=f"10.1/{i}", year="2020") for i in range(5)]
        self.resolved: list[AcademicItem] = []  # what a title lookup answers instead
        self.work_ids: dict[str, str] = {}  # doi -> its W id, what `get` answers

    def search(self, query, limit=10, filters=None):
        self.search_calls += 1
        if filters and filters.get("title"):  # a resolution, not the opening search
            return self.resolved
        return self.hits[:limit]

    def get(self, key):
        work_id = self.work_ids.get(str(key))
        if work_id is None:
            raise NotFound(str(key))
        return AcademicItem(source="openalex", key=work_id, title="t",
                            doi=str(key) if str(key).startswith("10.") else "",
                            extra={"openalex_id": f"https://openalex.org/{work_id}"})


class SurveyNewTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.backend = FakeBackend()
        self.cfg = {}
        patcher = mock.patch.object(survey, "get_all_backends",
                                    lambda cfg, corpora=False: {"openalex": self.backend})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def args(self, *argv):
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="survey_cmd")
        survey.add_new_parser(sub.add_parser("new"))
        parsed = parser.parse_args(["new", "wick", "boiling", *argv])
        parsed.root = str(self.root)
        return parsed

    def run_cmd(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = survey.cmd_survey_new(self.cfg, self.args(*argv))
        return code, out.getvalue()

    def only_project(self):
        dirs = [d for d in self.root.iterdir() if d.is_dir()]
        self.assertEqual(len(dirs), 1)
        return dirs[0]

    def test_only_the_slug_goes_to_stdout(self):
        code, out = self.run_cmd("--seed", "10.1002/x")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), self.only_project().name)
        self.assertTrue(out.strip().startswith("wick_boiling"))

    def test_filters_reach_the_project_file(self):
        self.run_cmd("--seed", "10.1002/x", "--year", "2019-2024", "--type", "review",
                     "--container", "Nature")
        p = project.load(self.only_project())
        self.assertEqual((p.year_from, p.year_to), ("2019", "2024"))
        self.assertEqual(p.type_weight, {"review": 2.0})
        self.assertEqual(p.container, "Nature")
        self.assertEqual(p.threshold, 2.5)
        self.assertEqual(p.effort, "medium")

    def test_an_open_ended_year_is_recognised(self):
        _code, out = self.run_cmd("--seed", "10.1002/x", "--year", "2019-")
        p = project.load(self.root / out.strip())
        self.assertEqual((p.year_from, p.year_to), ("2019", ""))
        _code, out = self.run_cmd("--seed", "10.1002/x", "--year", "-2024")
        p = project.load(self.root / out.strip())
        self.assertEqual((p.year_from, p.year_to), ("", "2024"))

    def test_a_seed_is_recorded_as_a_seed(self):
        self.run_cmd("--seed", "10.1002/x")
        p = project.load(self.only_project())
        self.assertEqual(p.seeds, ["10.1002/x"])
        rows = project.read_seen(p)
        self.assertEqual(rows[0]["identity"], "doi:10.1002/x")
        self.assertTrue(rows[0]["admit"])
        self.assertEqual(rows[0]["reason"], "seed")

    def test_seeds_skip_the_search(self):
        self.run_cmd("--seed", "10.1002/x")
        self.assertEqual(self.backend.search_calls, 0)

    def test_without_seeds_the_opening_search_supplies_three(self):
        self.run_cmd()
        self.assertEqual(self.backend.search_calls, 1)
        p = project.load(self.only_project())
        self.assertEqual(len(p.seeds), 3)
        self.assertEqual(len(project.read_seen(p)), 3)
        self.assertTrue(all(r["seed"] == "search" for r in project.read_seen(p)))

    def test_a_search_seed_is_stored_as_its_work_id(self):
        # The seed-citer bonus compares against graph references, which are work ids: a DOI
        # here could never match, and the bonus would be dead for every search seed.
        self.run_cmd()
        self.assertEqual(project.load(self.only_project()).seeds, ["W0", "W1", "W2"])

    def test_a_resolved_seed_is_stored_under_its_work_id(self):
        # Resolution hands back an openalex item whose key is its DOI (`doi or oid`), but the
        # seed-citer bonus compares against bare W-ids: storing the DOI keeps the bonus dead.
        self.backend.hits = [AcademicItem(source="arxiv", key="0702012",
                                          title="Saturated pool boiling", year="2007")]
        self.backend.resolved = [AcademicItem(source="openalex", key="10.1/77",
                                              title="Saturated pool boiling", year="2007",
                                              extra={"openalex_id": "https://openalex.org/W77"})]
        self.run_cmd()
        p = project.load(self.only_project())
        self.assertEqual(p.seeds, ["W77"])
        self.assertEqual([r["key"] for r in project.read_seen(p)], ["W77"])

    def test_an_explicit_doi_seed_is_stored_under_its_work_id(self):
        self.backend.work_ids = {"10.1002/xhoff": "W9"}
        self.run_cmd("--seed", "10.1002/xhoff")
        p = project.load(self.only_project())
        self.assertEqual(p.seeds, ["W9"])
        row = project.read_seen(p)[0]
        self.assertEqual(row["key"], "W9")
        self.assertEqual(row["identity"], "doi:10.1002/xhoff")  # what the user typed stays

    def test_an_explicit_seed_the_graph_does_not_know_keeps_its_spelling(self):
        self.backend.work_ids = {}
        self.run_cmd("--seed", "10.1002/xhoff")
        self.assertEqual(project.load(self.only_project()).seeds, ["10.1002/xhoff"])

    def test_a_prefix_typed_seed_loses_its_prefix(self):
        # `doi:` is not expandable as typed (`/works/doi:doi:10.x`), and the prefix would end
        # up inside the identity as well.
        self.backend.work_ids = {"10.9/y": "W8"}
        self.run_cmd("--seed", "doi:10.9/y")
        p = project.load(self.only_project())
        self.assertEqual(p.seeds, ["W8"])
        self.assertEqual(project.read_seen(p)[0]["identity"], "doi:10.9/y")

    def test_a_flag_seed_carries_the_doi_its_download_needs(self):
        # The seed is downloaded like any admitted work, so its row must carry a DOI even when
        # the user typed a work id.
        self.backend.work_ids = {"W7": "W7"}

        def get(key):
            return AcademicItem(source="openalex", key="W7", doi="10.7/z", title="Seeded",
                                extra={"openalex_id": "https://openalex.org/W7"})
        self.backend.get = get
        self.run_cmd("--seed", "W7")
        row = project.read_seen(project.load(self.only_project()))[0]
        self.assertEqual((row["key"], row["doi"], row["title"]), ("W7", "10.7/z", "Seeded"))

    def test_a_seed_from_another_source_is_resolved_to_an_openalex_work(self):
        # The walk expands OpenAlex ids only (`/works/<id>`); a seed straight from arXiv keeps
        # an arXiv id and 404s the very first hop, which is the default path, not an edge.
        self.backend.hits = [AcademicItem(source="arxiv", key="0702012",
                                          title="Saturated pool boiling", year="2007")]
        self.backend.resolved = [AcademicItem(source="openalex", key="W77", doi="10.1/77",
                                              title="Saturated pool boiling", year="2007")]
        self.run_cmd()
        p = project.load(self.only_project())
        self.assertEqual(p.seeds, ["W77"])
        self.assertEqual([r["key"] for r in project.read_seen(p)], ["W77"])

    def test_a_seed_that_cannot_be_resolved_is_dropped_with_a_note(self):
        self.backend.hits = [AcademicItem(source="arxiv", key="0702012", title="gone",
                                          year="2007"),
                             AcademicItem(source="openalex", key="W5", doi="10.1/5",
                                          title="kept", year="2020")]
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.run_cmd()
        self.assertEqual(project.load(self.only_project()).seeds, ["W5"])
        self.assertIn("0702012", err.getvalue())

    def test_a_doi_seed_from_another_source_is_kept_without_a_lookup(self):
        # ``/works/doi:…`` is as expandable as a W-id, so a DOI seed needs no conversion.
        self.backend.hits = [AcademicItem(source="crossref", key="10.1002/x", title="t",
                                          year="2020")]
        self.run_cmd()
        self.assertEqual(project.load(self.only_project()).seeds, ["10.1002/x"])
        self.assertEqual(self.backend.search_calls, 1)  # the opening search, no title lookup

    def test_a_seed_lookup_that_fails_once_is_retried(self):
        # This lookup is the entry point of the whole run and OpenAlex answers 429 on a burst;
        # the backend already retried what it counts as transport failure, so one more try here
        # costs a second and keeps the seed from silently losing its work id.
        self.backend.work_ids = {"10.9/y": "W8"}
        real, calls = self.backend.get, []

        def flaky(key):
            calls.append(key)
            if len(calls) == 1:
                raise BotWall("429 Too Many Requests")
            return real(key)

        self.backend.get = flaky
        self.run_cmd("--seed", "10.9/y")
        self.assertEqual(project.load(self.only_project()).seeds, ["W8"])

    def test_an_empty_opening_search_is_an_error(self):
        self.backend.hits = []
        code, out = self.run_cmd()
        self.assertEqual(code, 2)
        self.assertEqual(out, "")

    def test_an_explicit_threshold_and_effort_are_kept(self):
        self.run_cmd("--seed", "10.1002/x", "--threshold", "4", "--effort", "max")
        p = project.load(self.only_project())
        self.assertEqual(p.threshold, 4.0)
        self.assertEqual(p.effort, "max")

    def test_a_bad_effort_is_refused_by_the_parser(self):
        with self.assertRaises(SystemExit):
            self.run_cmd("--seed", "10.1002/x", "--effort", "insane")
    def test_a_seed_without_a_title_or_expandable_key_is_dropped(self):
        self.backend.hits = [AcademicItem(source="arxiv", key="0702012", title="", year="2007"),
                             AcademicItem(source="openalex", key="W5", title="kept", year="2020")]
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.run_cmd()
        self.assertEqual(project.load(self.only_project()).seeds, ["W5"])
        self.assertIn("no title", err.getvalue())
