"""A whole survey over a canned citation graph: no network, no browser, real accounting.

The graph is two works: ``W1`` cites and is related to ``W2``, and ``W2`` cites back to ``W1``
in nothing. That is enough for two hops — the seed, then its candidate — and enough to prove
the pieces fit: every judged candidate carries a reason, admitted ones land a copy in
``corpus/``, the quota only ever falls, and a hop answered entirely from the cache costs
nothing. The cache half is checked by walking a second project over the same graph.
"""
import argparse
import contextlib
import io
import itertools
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar
from unittest import mock

import httpx
from tenacity import wait_none

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from litcli import cache, project
from litcli.backends.openalex import OpenAlexBackend
from litcli.commands import survey
from tests.fake_browser import FakeSession


def work(wid: str, doi: str, year: int, cited: int, refs=(), related=()) -> dict:
    return {"id": f"https://openalex.org/{wid}", "doi": f"https://doi.org/{doi}",
            "title": f"Work {wid}", "publication_year": year, "cited_by_count": cited,
            "type": "article",
            "referenced_works": [f"https://openalex.org/{r}" for r in refs],
            "related_works": [f"https://openalex.org/{r}" for r in related]}


class Graph(httpx.BaseTransport):
    """Routes ``/works/<W>`` and ``filter=cites:<W>`` to canned records; records every URL."""

    WORKS: ClassVar[dict] = {"W1": work("W1", "10.1002/a", 2020, 30, refs=("W2",), related=("W2",)),
             "W2": work("W2", "10.1002/b", 2018, 20)}

    def __init__(self):
        self.urls: list[str] = []

    def handle_request(self, request):
        url = str(request.url)
        self.urls.append(url)
        if "filter=cites:" in url:
            wid = url.split("filter=cites:")[1].split("&")[0]
            hits = [self.WORKS[wid]] if wid == "W1" else []
            return httpx.Response(200, json={"results": hits}, request=request)
        wid = request.url.path.rsplit("/", 1)[-1]
        if wid in self.WORKS:
            return httpx.Response(200, json=self.WORKS[wid], request=request)
        return httpx.Response(404, json={"error": "not found"}, request=request)


class FakePublisher:
    name = "wiley"

    def __init__(self, session):
        self.session = session

    def matches_url(self, url):
        return True

    def get_attachment(self, doi):
        return b"%PDF-1.4\n%fake\n"


class SurveyE2ETest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self._cache = self.root / "cache"
        patcher = mock.patch("litcli.cache.cache_dir", return_value=self._cache)
        patcher.start()
        self.addCleanup(patcher.stop)
        disabled = cache._DISABLED
        cache._DISABLED = False
        self.addCleanup(setattr, cache, "_DISABLED", disabled)
        self.session = FakeSession()
        self.backend = OpenAlexBackend()
        self.backend.close()
        self.backend.cacheable = True
        self.backend.retry_wait = wait_none()
        self.backend._throttle = lambda: None
        self.graph = Graph()
        self.backend._transport = self.graph
        patcher = mock.patch.object(survey, "deps_from_config",
                                    lambda cfg, p: survey.Deps(
                                        backend=self.backend,
                                        resolve=lambda doi: {
                                            "has_fulltext": True, "kind": "wiley",
                                            "format": "pdf", "url": f"https://e.test/{doi}.pdf"},
                                        publishers={"wiley": FakePublisher(self.session)}))
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_project(self, name):
        project_ = project.create(self.root / name, "wick boiling", seeds=["doi:10.1002/a"],
                                  threshold=1.0, effort="low")
        project.append_seen(project_, {"identity": "doi:10.1002/a", "admit": True,
                                       "reason": "seed", "key": "W1", "doi": "10.1002/a",
                                       "score": 0.0, "parts": {}})
        return project_

    def run_cmd(self, project_, *extra):
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="survey_cmd")
        survey.add_run_parser(sub.add_parser("run"))
        args = parser.parse_args(["run", str(project_.root), *extra])
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = survey.cmd_survey_run({}, args)
        return code, out.getvalue()

    def hop_ends(self, project_):
        return [e for e in project.read_trace(project_) if e.get("event") == "hop_end"]

    def test_a_full_walk_leaves_a_reasoned_corpus(self):
        project_ = self.make_project("walk")
        code, _out = self.run_cmd(project_)
        self.assertEqual(code, 0)
        self.assertTrue(any((project_.root / "corpus").glob("*.pdf")))
        rows = project.read_seen(project_)
        self.assertTrue(all(row.get("reason") for row in rows))
        self.assertTrue(any(row.get("admit") and row.get("download") == "ok" for row in rows))

    def test_the_quota_never_rises_and_a_cached_hop_costs_nothing(self):
        first = self.make_project("first")
        self.run_cmd(first)
        remaining = [end["remaining"]["openalex"] for end in self.hop_ends(first)]
        self.assertTrue(all(b <= a for a, b in itertools.pairwise(remaining)), remaining)
        self.assertTrue(any(end.get("hits", 0) > 0 for end in self.hop_ends(first)))

        second = self.make_project("second")
        self.run_cmd(second)
        ends = self.hop_ends(second)
        self.assertTrue(ends[0].get("hits", 0) > 0)
        for previous, end in itertools.pairwise([ends[0], *ends]):
            if end.get("hits", 0) > 0:
                self.assertEqual(end["remaining"]["openalex"],
                                 previous["remaining"]["openalex"])

    def test_markdown_format_prints_tables_only(self):
        project_ = self.make_project("md")
        code, out = self.run_cmd(project_, "--format", "md")
        self.assertEqual(code, 0)
        self.assertIn("| score |", out)
        self.assertTrue(all(line.startswith("|") or not line.strip() or line.startswith("#")
                            for line in out.splitlines() if line.strip()))
