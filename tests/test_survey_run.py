"""`litcli survey run`: one hop at a time, resumable, never repeating a hop.

The loop is a checkpoint walk, so its failure modes are the checkpoint's: a resumed run must
expand only what was never expanded, a single project must have at most one writer, and a
download that fails must cost the copy but not the candidate. A gate on the download is a
missing copy, not a missing record; a session that dies mid-hop is retried through the cookie
path for that one download, and the record says which path got the file.
"""
import argparse
import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from litcli import project, survey_events
from litcli.commands import survey
from litcli.errors import BotWall
from litcli.models import AcademicItem
from tests.fake_browser import FakeSession


def raising(exc):
    def fail(*_args, **_kw):
        raise exc
    return fail


class FakeDiscovery:
    """An OpenAlex-shaped source: one candidate, answered for every expansion."""

    name = "openalex"
    capabilities: ClassVar[set[str]] = {"read"}

    def __init__(self):
        self.references_calls = 0
        self.citing_calls = 0
        self.related_calls = 0

    def _candidate(self):
        return AcademicItem(source="openalex", key="W1", doi="10.1002/x", title="A candidate",
                            year="2020", cited_by_count=40,
                            extra={"openalex_type": "article", "referenced_works": []})

    def references(self, key, limit=5):
        self.references_calls += 1
        return [self._candidate()]

    def citing(self, key, limit=5):
        self.citing_calls += 1
        return []

    def related(self, key, limit=5):
        self.related_calls += 1
        return []


class FakePublisher:
    """A publisher backend whose attachment needs the live session, like the real ones."""

    name = "wiley"

    def __init__(self, session):
        self.session = session

    def matches_url(self, url):
        return True

    def get_attachment(self, doi):
        if self.session is not None and not self.session.alive():
            raise BotWall("the session is gone")
        return b"%PDF-1.4\n%fake\n"


class SurveyRunTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.project = project.create(Path(self._tmp.name), "wick boiling",
                                      seeds=["10.1002/seed"], threshold=1.0, effort="low")
        project.append_seen(self.project, {"identity": "doi:10.1002/seed", "admit": True,
                                           "reason": "seed", "seed": "flag",
                                           "key": "10.1002/seed", "score": 0.0, "parts": {}})
        self.backend = FakeDiscovery()
        self.session = FakeSession()
        self.publisher = FakePublisher(self.session)
        self.deps = survey.Deps(
            backend=self.backend,
            resolve=lambda doi: {"has_fulltext": True, "kind": "wiley", "format": "pdf",
                                 "access": "entitled",
                                 "url": f"https://example.test/{doi}.pdf"},
            publishers={"wiley": self.publisher},
            session_alive=lambda: self.session.alive(),
        )
        self.cfg = {}
        patcher = mock.patch.object(survey, "deps_from_config", lambda cfg, p: self.deps)
        patcher.start()
        self.addCleanup(patcher.stop)

    def args(self):
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="survey_cmd")
        survey.add_run_parser(sub.add_parser("run"))
        parsed = parser.parse_args(["run", str(self.project.root)])
        return parsed

    def seen_for(self, doi):
        rows = [r for r in project.read_seen(self.project) if r.get("identity") == f"doi:{doi}"]
        self.assertTrue(rows, f"no seen row for {doi}")
        return rows[-1]

    def seed_trace_with_one_expanded_hop(self):
        """A checkpoint where the seed was already expanded and the candidate admitted."""
        project.append_seen(self.project, {"identity": "doi:10.1002/x", "admit": True,
                                           "reason": "score 2.00 >= 1.00", "hop": 1,
                                           "key": "W1", "doi": "10.1002/x", "score": 2.0,
                                           "parts": {}, "download": "ok"})
        project.append_trace(self.project, {"event": "start", "hop": 0})
        project.append_trace(self.project, {"event": "hop_end", "hop": 1, "new": 1,
                                            "expanded": ["doi:10.1002/seed"]})

    def test_resume_does_not_expand_a_hop_twice(self):
        self.seed_trace_with_one_expanded_hop()
        survey.run_loop(self.project, self.deps)
        self.assertEqual(self.backend.references_calls, 1)

    def test_a_gate_on_download_keeps_the_candidate(self):
        self.deps.resolve = raising(BotWall("gate"))
        survey.run_loop(self.project, self.deps)
        rec = self.seen_for("10.1002/x")
        self.assertTrue(rec["admit"])
        self.assertEqual(rec["download"], "unavailable")

    def test_streak_stop_is_reported_as_saturation(self):
        self.assertEqual(survey.run_loop(self.project, self.deps)["stop"], "saturated")

    def test_second_run_while_locked_exits_2(self):
        (self.project.root / "$.lock").write_text("1")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(survey.cmd_survey_run(self.cfg, self.args()), 2)

    def test_a_browser_that_dies_mid_hop_falls_back_for_that_download(self):
        self.session.alive = iter([True, False]).__next__
        survey.run_loop(self.project, self.deps)
        self.assertEqual(self.seen_for("10.1002/x")["download"], "cookie")
        self.assertIsNone(self.publisher.session)

    def test_a_stop_sentinel_ends_the_loop_before_a_hop(self):
        (self.project.root / "$.stop").write_text("1")
        self.assertEqual(survey.run_loop(self.project, self.deps)["stop"], "stopped")
        self.assertEqual(self.backend.references_calls, 0)
        self.assertFalse((self.project.root / "$.stop").exists())

    def test_every_judged_candidate_gets_a_trace_event(self):
        survey.run_loop(self.project, self.deps)
        trace = project.read_trace(self.project)
        candidates = [e for e in trace if e["event"] == "candidate"]
        self.assertTrue(candidates)
        self.assertTrue(all(e.get("reason") for e in candidates))
        self.assertEqual(trace[-1]["event"], "stop")

    def test_the_trace_renders_as_markdown(self):
        survey.run_loop(self.project, self.deps)
        rendered = survey_events.render_md(project.read_trace(self.project))
        self.assertIn("| score |", rendered)

    def test_a_downloaded_copy_lands_in_the_corpus(self):
        survey.run_loop(self.project, self.deps)
        files = [p for p in (self.project.root / "corpus").iterdir() if p.suffix == ".pdf"]
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0].read_bytes().startswith(b"%PDF"))
