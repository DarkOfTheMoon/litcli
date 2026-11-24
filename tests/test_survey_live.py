"""Live smoke: the survey path and the browser session against the real world (opt-in).

The offline suite pins shapes; this file walks a real seed through OpenAlex and the fulltext
chain, which is where page shapes, gates and transport actually break. It needs network (and,
for the session tests, Chrome), so it runs only when asked:

    LITCLI_LIVE=1 python3 -m unittest tests.test_survey_live -v

It writes into a temporary root and bypasses the metadata cache, so every answer is fresh.
Expect a few minutes and up to ~40 OpenAlex requests per run.
"""
import argparse
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import litcli.browser as browser
from litcli import config, project
from litcli.commands import browser as browser_cmd
from litcli.commands import survey

#: A heavily referenced, open-access paper: 115 references, an OA copy. Its DOI is what a
#: user would type; the walk has to store the work id it belongs to.
SEED_DOI = "10.1103/PhysRevLett.116.061102"
SEED_WORK = "W2252795400"
LIVE = os.environ.get("LITCLI_LIVE") == "1"


def _run(module, builder, argv, root):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    builder(sub.add_parser(argv[0]))
    args = parser.parse_args(argv)
    args.root = str(root)
    return args


@unittest.skipUnless(LIVE, "live smoke: set LITCLI_LIVE=1")
class SurveyLiveTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.cfg = config.load_config()
        for patcher in (mock.patch.dict(os.environ, {"LITCLI_NO_CACHE": "1"}),
                        mock.patch.object(survey, "paths", survey.paths)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def new(self, *argv):
        args = _run(survey, survey.add_new_parser, ["new", "gravitational", "waves", *argv],
                    self.root)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = survey.cmd_survey_new(self.cfg, args)
        self.assertEqual(code, 0, err.getvalue())
        return project.load(self.root / out.getvalue().strip())

    def run_project(self, survey_project, effort="low"):
        args = _run(survey, survey.add_run_parser, ["run", str(survey_project.root)], self.root)
        args.effort = effort
        args.format = "jsonl"
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = survey.cmd_survey_run(self.cfg, args)
        return code, out.getvalue(), err.getvalue()

    def test_a_doi_seed_is_stored_as_the_work_it_belongs_to(self):
        survey_project = self.new("--seed", SEED_DOI)
        self.assertEqual(survey_project.seeds, [SEED_WORK])

    def test_a_real_walk_leaves_a_walkable_checkpoint_and_copies(self):
        survey_project = self.new("--seed", SEED_DOI)
        code, _out, err = self.run_project(survey_project)
        trace = project.read_trace(survey_project)
        seen = project.read_seen(survey_project)
        kinds = [event.get("event") for event in trace]

        self.assertIn("hop_end", kinds, err)
        self.assertEqual(kinds[-1], "stop", err)
        self.assertTrue(all(row.get("reason") for row in seen), "a row without a reason")
        for row in seen:
            self.assertIn(row.get("download"), ("ok", "unavailable", "cookie", ""), row)
        landed = [row for row in seen if row.get("download") in ("ok", "cookie")]
        self.assertTrue(landed, "not one copy landed")
        for row in landed:
            self.assertTrue(list(project.corpus_dir(survey_project).iterdir()),
                            row["identity"])
        self.assertEqual(code, 0, err)
        self.assertFalse((survey_project.root / "$.lock").exists())

    def test_a_second_run_does_not_re_expand_what_the_first_did(self):
        # A resume legitimately expands works the first run admitted but never reached, so the
        # point is that no work is expanded twice, not that the total stays the same.
        survey_project = self.new("--seed", SEED_DOI)
        self.run_project(survey_project)
        first = [work for event in project.read_trace(survey_project)
                 if event.get("event") == "hop_end" for work in event.get("expanded") or []]
        self.assertTrue(first)
        self.run_project(survey_project)
        every = [work for event in project.read_trace(survey_project)
                 if event.get("event") == "hop_end" for work in event.get("expanded") or []]
        self.assertEqual(len(every), len(set(every)), "a resume re-expanded a finished hop")
        self.assertLessEqual(set(first), set(every))

    def test_status_reports_what_the_trace_holds(self):
        survey_project = self.new("--seed", SEED_DOI)
        self.run_project(survey_project)
        args = _run(survey, survey.add_status_parser,
                    ["status", str(survey_project.root)], self.root)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(survey.cmd_survey_status(self.cfg, args), 0)
        report = json.loads(out.getvalue())
        self.assertGreaterEqual(report["hops"], 1)
        self.assertGreaterEqual(report["admitted"], 1)
        self.assertGreaterEqual(report["corpus"], 1)
        self.assertFalse(report["running"])


def _has_chrome() -> bool:
    if not any(shutil.which(binary) for binary in browser._CHROME_BINARIES):
        return False
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False
    return True


@unittest.skipUnless(LIVE, "live smoke: set LITCLI_LIVE=1")
@unittest.skipUnless(_has_chrome(), "the session tests need Chrome and playwright")
class BrowserStatusLiveTest(unittest.TestCase):
    def test_every_site_answers_with_a_state(self):
        args = _run(browser_cmd, lambda p: None, ["status"], Path("/tmp"))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = browser_cmd.cmd_browser_status(config.load_config(), args)
        self.assertEqual(code, 0, err.getvalue())
        lines = [line for line in out.getvalue().splitlines() if line.strip()]
        self.assertEqual(len([line for line in lines if "  " in line]), len(browser_cmd.SITES))
        self.assertNotIn("error:", out.getvalue())
