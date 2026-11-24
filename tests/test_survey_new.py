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
from litcli.models import AcademicItem


class FakeBackend:
    name = "openalex"

    def __init__(self):
        self.search_calls = 0
        self.hits = [AcademicItem(source="openalex", key=f"W{i}", title=f"hit {i}",
                                  doi=f"10.1/{i}", year="2020") for i in range(5)]

    def search(self, query, limit=10, filters=None):
        self.search_calls += 1
        return self.hits[:limit]


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
