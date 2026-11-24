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
                                           "reason": "seed", "score": 0.0, "parts": {}})

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
                         effort.resolve(self.project.effort)["openalex_cap"])
        self.assertEqual(report["question"], "wick boiling")
        self.assertEqual(report["status"], "running")
        self.assertEqual(report["hops"], 0)
        self.assertEqual(report["admitted"], 1)
        self.assertEqual(report["last_stop"], "")

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
