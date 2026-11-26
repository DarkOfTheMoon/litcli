"""`litcli survey status|stop`: the read-only view and the sentinel that ends a walk.

An agent checks status between steps and stops a project another process is walking, so status
is machine-readable JSON with the remaining envelope, and stop writes a file rather than
signalling a process it does not own. Stopping an already-stopped project is a success, not an
error — the caller's intent is already satisfied.
"""
import argparse
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from litcli import effort, project
from litcli.commands import survey


class StatusStopTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.project = project.create(self.root, "wick boiling", seeds=["10.1002/seed"])
        project.append_seen(self.project, {"identity": "doi:10.1002/seed", "admit": True,
                                           "reason": "seed", "key": "10.1002/seed",
                                           "title": "Honeycomb sandwich panels",
                                           "score": 0.0, "parts": {}})

    def args(self, command, *argv):
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="survey_cmd")
        getattr(survey, f"add_{command}_parser")(sub.add_parser(command))
        return parser.parse_args([command, str(self.project.root), *argv])

    def run_cmd(self, command, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = getattr(survey, f"cmd_survey_{command}")({}, self.args(command, *argv))
        return code, out.getvalue(), err.getvalue()

    def test_status_is_json_with_the_remaining_envelope(self):
        code, out, _err = self.run_cmd("status")
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual(report["remaining"]["openalex"],
                         effort.resolve(self.project.effort)["graph_cap"])
        self.assertEqual(report["question"], "wick boiling")
        self.assertEqual(report["status"], "running")
        self.assertEqual(report["hops"], 0)
        self.assertEqual(report["admitted"], 1)
        self.assertEqual(report["last_stop"], "")

    def test_status_lists_the_seeds_with_their_question_coverage(self):
        # The seeds decide the walk and skip both filters; a reader has to be able to see that
        # they are off the question without opening seen.jsonl.
        code, out, _err = self.run_cmd("status")
        self.assertEqual(code, 0)
        seeds = json.loads(out)["seeds"]
        self.assertEqual([row["identity"] for row in seeds], ["doi:10.1002/seed"])
        self.assertEqual(seeds[0]["title"], "Honeycomb sandwich panels")
        self.assertAlmostEqual(seeds[0]["cover"], 0.0)

    def test_a_migrated_floor_is_reported_as_migrated(self):
        # The floor 0.2 shown for a legacy project was never in the file: it is what the old
        # score value was replaced with, and saying only "0.2" hides that.
        path = self.project.root / "project.toml"
        path.write_text(path.read_text(encoding="utf-8").replace("floor = 0.2", "threshold = 2.5"),
                        encoding="utf-8")
        _code, out, _err = self.run_cmd("status")
        report = json.loads(out)
        self.assertEqual(report["floor"], project.DEFAULT_FLOOR)
        self.assertEqual(report["floor_legacy"], "threshold")
        _code, why, _err = self.run_cmd("status", "--why")
        self.assertIn("threshold", why)

    def test_a_current_project_reports_no_legacy_floor(self):
        _code, out, _err = self.run_cmd("status")
        self.assertIsNone(json.loads(out)["floor_legacy"])

    def test_why_shows_the_last_hop_best_evidence_first(self):
        project.append_seen(self.project, {"identity": "doi:10.1/a", "admit": True,
                                           "reason": "evidence 0.40 >= 0.20", "hop": 2,
                                           "evidence": 0.4, "score": 1.0, "title": "on topic"})
        project.append_seen(self.project, {"identity": "doi:10.1/b", "admit": False,
                                           "reason": "evidence 0.05 < 0.20", "hop": 2,
                                           "evidence": 0.05, "score": 4.9, "title": "off topic"})
        _code, out, _err = self.run_cmd("status", "--why")
        admitted = out.index("on topic")
        refused = out.index("off topic")
        self.assertLess(admitted, refused)
        self.assertIn("0.40", out)
        self.assertIn("0.05", out)

    def test_why_marks_the_seed_row_itself(self):
        # The summary line says `weak: N` whatever the rows do; this is the row's own mark.
        _code, out, _err = self.run_cmd("status", "--why")
        self.assertIn("  weak 0.00  doi:10.1002/seed  Honeycomb sandwich panels", out)

    def test_the_seed_identity_is_the_seen_rows_own(self):
        # `seeds[].identity` used to be the project's walk id (`W…` / a bare DOI), which no
        # `seen.jsonl` row carries: a consumer joining the two found nothing.
        _code, out, _err = self.run_cmd("status")
        self.assertEqual([row["identity"] for row in json.loads(out)["seeds"]],
                         ["doi:10.1002/seed"])

    def test_a_seed_with_no_title_reports_an_unknown_coverage(self):
        # A DOI whose lookup failed has no title to score. Reporting 0.00 calls it off the
        # question when nothing is known about it.
        self.project = project.create(self.root, "wick boiling", seeds=["10.9/unknown"])
        project.append_seen(self.project, {"identity": "doi:10.9/unknown", "admit": True,
                                           "reason": "seed", "key": "10.9/unknown",
                                           "title": "", "score": 0.0, "parts": {}})
        _code, out, _err = self.run_cmd("status")
        seed = json.loads(out)["seeds"][0]
        self.assertIsNone(seed["cover"])
        self.assertFalse(seed["weak"])
        _code, why, _err = self.run_cmd("status", "--why")
        self.assertIn("  ?       ?  doi:10.9/unknown", why)

    def test_why_says_how_many_rows_it_did_not_show(self):
        # The view stops at WHY_ROWS; without the count the reader cannot tell a truncated
        # hop from a short one.
        for i in range(survey.WHY_ROWS + 1):
            project.append_seen(self.project, {"identity": f"doi:10.1/{i}", "admit": False,
                                               "reason": "evidence 0.00 < 0.20", "hop": 1,
                                               "evidence": 0.0, "score": 1.0,
                                               "title": f"row {i}"})
        _code, out, _err = self.run_cmd("status", "--why")
        self.assertIn("1 more not shown", out)

    def test_seeds_with_the_same_doi_tail_are_not_confused(self):
        # `scoring.bare` keeps only the last path segment, so two DOIs that share their tail
        # used to read the other's title (and coverage) into each other's row.
        self.project = project.create(self.root, "wick boiling",
                                      seeds=["10.1002/abc", "10.1016/abc"])
        for doi, title in (("10.1002/abc", "first seed"), ("10.1016/abc", "second seed")):
            project.append_seen(self.project, {"identity": "doi:" + doi, "admit": True,
                                               "reason": "seed", "key": doi, "title": title,
                                               "score": 0.0, "parts": {}})
        _code, out, _err = self.run_cmd("status")
        titles = {row["identity"]: row["title"] for row in json.loads(out)["seeds"]}
        self.assertEqual(titles, {"doi:10.1002/abc": "first seed",
                                  "doi:10.1016/abc": "second seed"})

    def test_why_says_nothing_is_judged_yet_on_a_new_project(self):
        # At hop 0 the only rows are the seeds, which are never scored: counting them as
        # "judged, admitted" candidates reads as if the walk had already ruled on something.
        _code, why, _err = self.run_cmd("status", "--why")
        self.assertIn("nothing has been judged", why)
        self.assertNotIn("judged,", why)

    def test_why_names_the_true_last_hop(self):
        # A barren hop judges nothing, so the highest hop among the rows is not the last hop;
        # the help text promises the last hop's candidates.
        project.append_seen(self.project, {"identity": "doi:10.1/a", "admit": True,
                                           "reason": "evidence 0.40 >= 0.20", "hop": 1,
                                           "evidence": 0.4, "score": 1.0, "title": "on topic"})
        project.append_trace(self.project, {"event": "hop_end", "hop": 1})
        project.append_trace(self.project, {"event": "hop_end", "hop": 2})
        _code, why, _err = self.run_cmd("status", "--why")
        self.assertIn("hop 2: no candidate was judged", why)

    def test_why_is_text_not_json(self):
        _code, out, _err = self.run_cmd("status", "--why")
        with self.assertRaises(ValueError):
            json.loads(out)

    def test_status_reads_the_last_checkpoint(self):
        project.append_trace(self.project, {"event": "start", "hop": 0})
        project.append_trace(self.project, {"event": "hop_end", "hop": 1, "new": 2,
                                            "expanded": [], "remaining": {"openalex": 7}})
        _code, out, _err = self.run_cmd("status")
        report = json.loads(out)
        self.assertEqual(report["remaining"]["openalex"], 7)
        self.assertEqual(report["hops"], 1)

    def test_stop_writes_the_sentinel(self):
        code, out, err = self.run_cmd("stop")
        self.assertEqual(code, 0)
        self.assertTrue((self.project.root / "$.stop").exists())
        self.assertIn("stop", out + err)

    def test_a_second_stop_succeeds_and_says_so(self):
        self.run_cmd("stop")
        code, out, err = self.run_cmd("stop")
        self.assertEqual(code, 0)
        self.assertIn("already", out + err)

    def test_status_reports_a_finished_walk(self):
        project.append_trace(self.project, {"event": "stop", "reason": "saturated", "admitted": 3})
        self.project.status = "done"
        project.save(self.project)
        _code, out, _err = self.run_cmd("status")
        report = json.loads(out)
        self.assertEqual(report["last_stop"], "saturated")
        self.assertEqual(report["status"], "done")

    def test_an_unknown_project_is_an_error(self):
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="survey_cmd")
        survey.add_status_parser(sub.add_parser("status"))
        args = parser.parse_args(["status", str(self.root / "nope")])
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(survey.cmd_survey_status({}, args), 2)
