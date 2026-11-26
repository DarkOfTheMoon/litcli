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
import signal
import sys
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from litcli import effort, project, survey_events
from litcli.commands import survey
from litcli.errors import BadInput, BotWall, NotFound, Unavailable
from litcli.models import AcademicItem
from litcli.transport import HttpError
from tests.fake_browser import FakeSession


def raising(exc):
    def fail(*_args, **_kw):
        raise exc
    return fail


class FakeDiscovery:
    """An OpenAlex-shaped source: one candidate, answered for every expansion."""

    name = "openalex"
    capabilities: ClassVar[set[str]] = {"read", "refs", "citing", "related"}

    def __init__(self, name=None):
        # Default to the class's own name, or a subclass's `name = "..."` would be clobbered.
        self.name = name or type(self).name
        self.references_calls = 0
        self.citing_calls = 0
        self.related_calls = 0

    def _candidate(self):
        return AcademicItem(source=self.name, key="W1", doi="10.1002/x", title="A candidate",
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


class WalledDiscovery(FakeDiscovery):
    """The preferred source while its budget is spent: every axis answers 429."""

    def references(self, key, limit=5):
        raise Unavailable("[openalex] HTTP 429: Too Many Requests")

    citing = related = references


class NeighbourOnly(FakeDiscovery):
    """A source that answers the neighbour axis alone: one charge per row, not three."""

    name = "neighbour"
    capabilities: ClassVar[set[str]] = {"read", "related"}

    def references(self, key, limit=5):
        raise survey.NotSupportedError("references", self)

    citing = references


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


class FakeLiveSession:
    """A session that is not running until started, like the real one on a cold machine."""

    def __init__(self):
        self.starts = 0
        self.terminations = 0
        self._up = False

    def alive(self):
        return self._up

    def start(self):
        self.starts += 1
        self._up = True

    def download(self, url):
        return b"%PDF-1.4\nbrowser\n"

    def close(self):
        self._up = False

    def terminate(self):
        self.terminations += 1
        self.close()


class DepsFromConfigTest(unittest.TestCase):
    def build(self, cfg, backends):
        from litcli.models import AcademicItem  # noqa: F401  (import side effect only)

        class Discovery:
            name = "openalex"

        with mock.patch.object(survey, "get_all_backends", lambda c, corpora=False: backends):
            return survey.deps_from_config(cfg, mock.Mock(root=Path("/tmp")))

    def test_an_enabled_browser_is_the_survey_session(self):
        # The browser is what gets past a gated OA link, and that has nothing to do with a
        # publisher subscription: with `[browser] enabled` a survey has to carry one even when
        # no publisher is configured at all.
        session = FakeLiveSession()
        with mock.patch.object(survey, "session_from_config", lambda cfg: session):
            deps = self.build({"browser": {"enabled": True}}, {"openalex": mock.Mock()})
        self.assertIsNotNone(deps.browser_fetch)
        self.assertTrue(deps.session_alive())  # and starting it is the first check's job
        self.assertEqual(session.starts, 1)
        self.assertEqual(deps.browser_fetch("https://e.test/x.pdf"), b"%PDF-1.4\nbrowser\n")

    def test_the_graph_sources_follow_a_fixed_preference(self):
        # OpenAlex first (it has the richest graph), then the services that can stand in when
        # its shared budget is gone: S2's graph, then the CC0 link service, then the deposits.
        seen: list[str] = []

        def optional(name, cfg):
            seen.append(name)
            made = mock.Mock()
            made.name = name
            return made

        with mock.patch("litcli.commands.keys.optional_backend", optional), \
                mock.patch.object(survey, "session_from_config", lambda cfg: None):
            graph = {}
            for label in ("openalex", "crossref"):
                graph[label] = mock.Mock()
                graph[label].name = label
            deps = self.build({}, graph)
        self.assertEqual([b.name for b in deps.sources()],
                         ["openalex", "semanticscholar", "opencitations", "crossref",
                          "europepmc"])
        self.assertEqual(seen, ["semanticscholar", "opencitations", "europepmc"])

    def test_a_source_claiming_an_axis_it_never_implemented_is_dropped(self):
        # Otherwise the first hop dies on an AttributeError, which used to escape the run.
        class Broken:
            name = "semanticscholar"
            capabilities = frozenset({"read", "refs"})

        with mock.patch("litcli.commands.keys.optional_backend", lambda name, cfg: None), \
                mock.patch.object(survey, "session_from_config", lambda cfg: None):
            deps = self.build({}, {"openalex": FakeDiscovery(), "semanticscholar": Broken()})
        self.assertEqual([b.name for b in deps.sources()], ["openalex"])

    def test_an_openalex_only_the_fallback_built_is_still_the_deps_backend(self):
        # `keys` adds an unconfigured openalex as a keyless fallback; the OA-location lookup
        # and the fulltext chain hang off `deps.backend`, which stayed None here — so the
        # walk's own OA links silently vanished although the instance was right there.
        oa = mock.Mock()
        oa.name = "openalex"

        def optional(name, cfg):
            return oa if name == "openalex" else None

        cross = mock.Mock()
        cross.name = "crossref"
        with mock.patch("litcli.commands.keys.optional_backend", optional), \
                mock.patch.object(survey, "session_from_config", lambda cfg: None):
            deps = self.build({}, {"crossref": cross})
        self.assertIs(deps.backend, oa)

    def test_a_keyless_graph_source_participates_unconfigured(self):
        # Nothing configured but crossref: S2 and OpenCitations are keyless, so the walk still
        # has a graph instead of dying on the 429 of a source nobody can now reach.
        def optional(name, cfg):
            if name != "semanticscholar":
                return None
            made = mock.Mock()
            made.name = name
            return made

        with mock.patch("litcli.commands.keys.optional_backend", optional), \
                mock.patch.object(survey, "session_from_config", lambda cfg: None):
            only = mock.Mock()
            only.name = "crossref"
            deps = self.build({}, {"crossref": only})
        self.assertIsNone(deps.backend)
        self.assertEqual([b.name for b in deps.sources()], ["semanticscholar", "crossref"])

    def test_no_graph_source_at_all_is_an_error_that_names_them(self):
        with mock.patch("litcli.commands.keys.optional_backend", lambda name, cfg: None), \
                mock.patch.object(survey, "session_from_config", lambda cfg: None), \
                self.assertRaises(BadInput) as caught:
            self.build({}, {})
        self.assertIn("semanticscholar", str(caught.exception))

    def test_the_run_stops_the_browser_it_started(self):
        # A survey run is one of many, and `close` only detaches: left at that, every run
        # leaves a browser behind, forever. `terminate` stops what this process started and
        # leaves one it merely attached to alone — which is what keeps a signed-in browser
        # the user opened up by hand.
        session = FakeLiveSession()
        with mock.patch.object(survey, "session_from_config", lambda cfg: session):
            deps = self.build({"browser": {"enabled": True}}, {"openalex": mock.Mock()})
        deps.session_alive()
        deps.close()
        self.assertEqual(session.terminations, 1)

    def test_a_reused_publisher_session_is_stopped_by_the_run_too(self):
        from litcli.backends._entitled import EntitledBackend

        class Publisher(EntitledBackend):
            def get(self, key):
                raise NotImplementedError

        shared = FakeLiveSession()
        publisher = object.__new__(Publisher)
        publisher.session = shared
        with mock.patch.object(survey, "session_from_config",
                               mock.Mock(side_effect=AssertionError("built a second one"))):
            deps = self.build({"browser": {"enabled": True}},
                              {"openalex": mock.Mock(), "wiley": publisher})
        deps.close()
        self.assertEqual(shared.terminations, 1)

    def test_without_the_browser_section_nothing_starts(self):
        deps = self.build({}, {"openalex": mock.Mock()})
        self.assertIsNone(deps.browser_fetch)
        self.assertTrue(deps.session_alive())

    def test_a_publisher_session_is_reused_rather_than_replaced(self):
        from litcli.backends._entitled import EntitledBackend

        class Publisher(EntitledBackend):
            def get(self, key):
                raise NotImplementedError

        shared = FakeLiveSession()
        publisher = object.__new__(Publisher)
        publisher.session = shared
        with mock.patch.object(survey, "session_from_config",
                               mock.Mock(side_effect=AssertionError("built a second one"))):
            deps = self.build({"browser": {"enabled": True}},
                              {"openalex": mock.Mock(), "wiley": publisher})
        self.assertIs(deps.browser_fetch.__self__, shared)


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
            session_alive=self.session_still_alive,
        )
        self.cfg = {}
        patcher = mock.patch.object(survey, "deps_from_config", lambda cfg, p: self.deps)
        patcher.start()
        self.addCleanup(patcher.stop)

    def session_still_alive(self):
        """A finite iterator in a test stands for "dies then stays dead"."""
        try:
            return self.session.alive()
        except StopIteration:
            return False

    def args(self):
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="survey_cmd")
        survey.add_run_parser(sub.add_parser("run"))
        parsed = parser.parse_args(["run", str(self.project.root)])
        return parsed

    def seen_for(self, doi):
        rows = [r for r in project.read_seen(self.project)
                if r.get("identity") == f"doi:{doi}" and r.get("admit")]
        self.assertTrue(rows, f"no admitted seen row for {doi}")
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

    def test_a_walled_source_hands_the_hop_to_the_next_one(self):
        # The whole point of the chain: a spent budget on the first source is not the hop's end.
        walled, good = WalledDiscovery(), FakeDiscovery("semanticscholar")
        self.deps.graph, self.deps.by_name = [walled, good], {"semanticscholar": good}
        survey.run_loop(self.project, self.deps)
        row = self.seen_for("10.1002/x")
        self.assertEqual(row["source"], "semanticscholar")
        ends = [e for e in project.read_trace(self.project) if e.get("event") == "hop_end"]
        # `citing`/`related` came back empty from the stand-in, so the wall did cost the row
        # two directions; it is retried rather than written down as a whole expansion.
        self.assertEqual(ends[0]["errored"], 1)

    def test_a_row_where_every_source_walled_keeps_its_place(self):
        self.deps.graph = [WalledDiscovery()]
        survey.run_loop(self.project, self.deps)
        ends = [e for e in project.read_trace(self.project) if e.get("event") == "hop_end"]
        self.assertEqual((ends[0]["errored"], ends[0]["expanded"]), (1, []))
        self.assertFalse([r for r in project.read_seen(self.project)
                          if r.get("identity") == "doi:10.1002/x"])

    def test_a_run_that_landed_a_copy_exits_0(self):
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(survey.cmd_survey_run(self.cfg, self.args()), 0)

    def test_the_run_disconnects_the_browser_it_used(self):
        # The session is built per run; an engine left attached keeps a Playwright driver busy
        # in the process and the next run's fresh one cannot start.
        closed = []
        self.deps.close = lambda: closed.append(True)
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            survey.cmd_survey_run(self.cfg, self.args())
        self.assertEqual(closed, [True])

    def test_a_run_that_only_got_metadata_exits_1(self):
        # Every admitted candidate gets a JSON sidecar in corpus/ before its download is even
        # tried, so "corpus/ is not empty" is true after any admission: the exit code has to
        # answer for landed copies, which is what "no copy at all" means.
        self.deps.resolve = raising(BotWall("gate"))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(survey.cmd_survey_run(self.cfg, self.args()), 1)
        self.assertTrue(list((self.project.root / "corpus").glob("*.json")))

    def test_a_browser_timeout_keeps_the_candidate_and_the_run(self):
        # A handle that 302s to a repository accepting `application/pdf` and then never
        # answering raises playwright's own TimeoutError, which is not an OSError: uncaught it
        # ended the whole run at hop 0, on a seed, with nothing walked.
        from litcli.browser import PlaywrightTimeoutError

        self.deps.publishers = {}  # the browser is the only route left, as it was on the handle
        self.deps.browser_fetch = raising(PlaywrightTimeoutError("Timeout 30000ms exceeded"))
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(survey.cmd_survey_run(self.cfg, self.args()), 1)
        self.assertEqual(self.seen_for("10.1002/seed")["download"], "unavailable")
        self.assertTrue(self.seen_for("10.1002/x"))
        self.assertTrue(list((self.project.root / "corpus").glob("*.json")))

    def test_a_download_that_raises_anything_still_lands_its_row(self):
        with mock.patch.object(survey, "_download",
                               raising(TimeoutError("Timeout 30000ms exceeded"))), \
                contextlib.redirect_stderr(io.StringIO()), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(survey.cmd_survey_run(self.cfg, self.args()), 1)
        self.assertEqual(self.seen_for("10.1002/seed")["download"], "error:TimeoutError")

    def test_a_copy_that_landed_after_the_browser_died_counts_as_landed(self):
        # The cookie retry records its copy as `cookie`, not `ok`: the exit code has to see it.
        self.session.alive = iter([True, False]).__next__
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(survey.cmd_survey_run(self.cfg, self.args()), 0)
        self.assertTrue(list((self.project.root / "corpus").glob("*.pdf")))

    def test_every_row_gets_the_per_work_edge_allowance(self):
        # `refs`/`citing`/`related` are edges read from EACH work; sharing one counter across
        # the whole run means the first row with a normal bibliography eats the level, and
        # every later row reads no edges at all while the site cap is still untouched.
        asked = []
        real = self.backend.references

        def references(key, limit=5):
            asked.append(key)
            n = len(asked)
            return [AcademicItem(source="openalex", key=f"W{n}{i}", doi=f"10.9/{n}{i}",
                                 title=f"t{n}{i}", year="2020", cited_by_count=40,
                                 extra={"openalex_type": "article", "referenced_works": []})
                    for i in range(min(limit, 10))]

        self.backend.references = references
        survey.run_loop(self.project, self.deps)
        self.assertGreaterEqual(len(asked), 2, "a later row was left with no edge allowance")
        self.assertEqual(real.__name__, "references")

    def test_a_gated_open_access_link_is_fetched_through_the_browser(self):
        # Unpaywall answers many DOIs with an OA PDF url on a publisher host that sits behind
        # its bot gate (link.aps.org does): plain HTTP gets a 403, while the same url in the
        # live browser is one page request away from the bytes.
        self.deps.publishers = {}
        self.deps.resolve = lambda doi: {"has_fulltext": True, "kind": "unpaywall",
                                         "format": "pdf",
                                         "url": f"https://link.aps.org/pdf/{doi}"}
        asked = []
        self.deps.browser_fetch = lambda url: asked.append(url) or b"%PDF-1.4\n%live\n"
        patcher = mock.patch.object(survey, "download_bytes", raising(BotWall("403")))
        patcher.start()
        self.addCleanup(patcher.stop)
        survey.run_loop(self.project, self.deps)
        self.assertEqual(self.seen_for("10.1002/x")["download"], "ok")
        self.assertIn("https://link.aps.org/pdf/10.1002/x", asked)

    def test_a_gated_answer_falls_back_to_the_works_open_access_attachment(self):
        # Unpaywall answers a lot of DOIs with a publisher url that answers only a browser, and
        # the chain therefore never asks OpenAlex: the work's own OA attachment (arXiv here)
        # has to be the next thing tried, not the end of the download.
        self.deps.publishers = {}
        gated = "https://link.aps.org/pdf/10.1002/x"
        self.deps.resolve = lambda doi: {"has_fulltext": True, "kind": "unpaywall",
                                         "format": "pdf", "url": gated}
        self.backend.get = lambda key: AcademicItem(
            source="openalex", key="W1", doi="10.1002/x", title="t",
            attachments=["https://arxiv.org/pdf/1602.03837"])
        self.deps.browser_fetch = lambda url: (b"%PDF-1.4\narxiv\n"
                                               if "arxiv" in url else raising(BotWall("gate"))())
        patcher = mock.patch.object(survey, "download_bytes", raising(BotWall("403")))
        patcher.start()
        self.addCleanup(patcher.stop)
        survey.run_loop(self.project, self.deps)
        self.assertEqual(self.seen_for("10.1002/x")["download"], "ok")
        self.assertIn(b"arxiv", (self.project.root / "corpus" / "10.1002_x.pdf").read_bytes())

    def test_plain_http_is_tried_before_the_browser(self):
        self.deps.publishers = {}
        self.deps.resolve = lambda doi: {"has_fulltext": True, "kind": "unpaywall",
                                         "format": "pdf", "url": f"https://e.test/{doi}.pdf"}
        asked = []
        self.deps.browser_fetch = lambda url: asked.append(url) or b"%PDF-1.4\n%live\n"
        patcher = mock.patch.object(survey, "download_bytes",
                                    lambda url: b"%PDF-1.4\n%plain\n")
        patcher.start()
        self.addCleanup(patcher.stop)
        survey.run_loop(self.project, self.deps)
        self.assertEqual(self.seen_for("10.1002/x")["download"], "ok")
        self.assertEqual(asked, [])  # no publisher matched, and plain HTTP answered

    def test_a_gate_on_download_keeps_the_candidate(self):
        self.deps.resolve = raising(BotWall("gate"))
        survey.run_loop(self.project, self.deps)
        rec = self.seen_for("10.1002/x")
        self.assertTrue(rec["admit"])
        self.assertEqual(rec["download"], "unavailable")

    def test_streak_stop_is_reported_as_saturation(self):
        self.assertEqual(survey.run_loop(self.project, self.deps)["stop"], "saturated")

    def test_a_rate_limited_hop_does_not_spend_the_streak(self):
        # A 429 burst answers the expansion with an error and no candidates. Read as an empty
        # hop, two of them in a row ended the run as `saturated` (rc 0) one hop in, on a topic
        # nothing had actually looked at yet.
        self.backend.references = raising(Unavailable("[openalex] HTTP 429"))
        result = survey.run_loop(self.project, self.deps)
        hops = [e for e in project.read_trace(self.project) if e["event"] == "hop_end"]
        self.assertGreater(len(hops), 2)  # `low` streaks at two empty hops
        self.assertEqual(result["stop"], "max_hops")
        self.assertTrue(all(e.get("errored") for e in hops))

    def test_a_duplicated_seen_row_is_expanded_once(self):
        # The download pass writes the row back as a NEW seen row, so an identity that already
        # landed has several admitted rows after a resume; handed to a hop one per row, the same
        # work was expanded twice in one hop (and its whole bibliography read twice).
        project.append_seen(self.project, {"identity": "doi:10.1002/seed", "admit": True,
                                           "reason": "seed", "key": "10.1002/seed",
                                           "score": 0.0, "parts": {}, "download": "ok"})
        survey.run_loop(self.project, self.deps)
        first = next(e for e in project.read_trace(self.project) if e["event"] == "hop_end")
        self.assertEqual(first["expanded"], ["doi:10.1002/seed"])

    def test_a_hop_that_only_errored_leaves_its_rows_unexpanded(self):
        # An errored row is written back to the frontier in memory; `expanded` is what a
        # resume reads, so it must not claim the row either — otherwise the interruption a 429
        # burst usually causes is exactly what loses the row.
        self.backend.references = raising(Unavailable("[openalex] HTTP 429"))
        survey.run_loop(self.project, self.deps)
        hops = [e for e in project.read_trace(self.project) if e["event"] == "hop_end"]
        self.assertTrue(hops)
        self.assertEqual([e["expanded"] for e in hops], [[] for _ in hops])
        self.assertEqual([e["abandoned"] for e in hops], [[] for _ in hops])

    def test_a_trace_from_before_the_fix_still_resumes(self):
        # The broken accounting wrote 429-killed rows into `expanded`, so those projects stop on
        # `saturated` at hop 0 with zero requests. The `error` event of the same hop is the
        # signal that the write was a lie, and it is already in the old traces.
        project.append_trace(self.project, {"event": "start", "hop": 0})
        project.append_trace(self.project, {"event": "error", "hop": 1,
                                            "identity": "doi:10.1002/seed", "reason": "429"})
        project.append_trace(self.project, {"event": "hop_end", "hop": 1, "new": 0,
                                            "expanded": ["doi:10.1002/seed"], "errored": 1})
        survey.run_loop(self.project, self.deps)
        self.assertGreater(self.backend.references_calls, 0)

    def test_a_dead_row_gets_one_retry_and_a_throttled_one_keeps_its_place(self):
        # `HttpError` is itself an `Unavailable`, so which of the two a row is has to be read
        # off the status: a 404 is the row's own fault and earns exactly one more hop, a 429 is
        # the source's mood and the row keeps its place while the envelope has hops left.
        for status, stop_reason in ((404, "saturated"), (429, "max_hops")):
            with self.subTest(status=status):
                for stray in list(self.project.root.glob("seen.jsonl")):
                    stray.unlink()
                soup = self.project.root / "trace.jsonl"
                if soup.exists():
                    soup.unlink()
                project.append_seen(self.project, {"identity": "doi:10.1002/seed", "admit": True,
                                                   "reason": "seed", "seed": "flag",
                                                   "key": "10.1002/seed", "score": 0.0, "parts": {}})
                asked = []

                def boom(key, limit=5, _asked=asked, _status=status):
                    _asked.append(key)
                    raise HttpError("openalex", _status, "nope")

                self.backend.references = boom
                self.assertEqual(survey.run_loop(self.project, self.deps)["stop"], stop_reason)
                if status == 404:
                    self.assertEqual(len(asked), 2)
                else:
                    self.assertGreater(len(asked), 2)

    def test_second_run_while_locked_exits_2(self):
        (self.project.root / "$.lock").write_text("1")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(survey.cmd_survey_run(self.cfg, self.args()), 2)

    def test_a_hand_broken_record_stops_the_run_at_its_line(self):
        # A torn seen.jsonl is what a killed `append` (or a hand edit) leaves behind; the run
        # has to name the file and the line instead of looping on half an answer.
        (self.project.root / "seen.jsonl").write_text('{"identity": "doi:x"}\n{"identity":\n',
                                                      encoding="utf-8")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(survey.cmd_survey_run(self.cfg, self.args()), 2)
        self.assertIn("seen.jsonl:2", err.getvalue())
        self.assertFalse((self.project.root / "$.lock").exists())

    def test_a_browser_that_dies_mid_hop_falls_back_for_that_download(self):
        self.session.alive = iter([True, False]).__next__
        survey.run_loop(self.project, self.deps)
        self.assertEqual(self.seen_for("10.1002/x")["download"], "cookie")
        self.assertIsNone(self.publisher.session)

    def test_a_download_cut_by_the_dying_browser_retries_without_the_session(self):
        # A window that closes while the bytes are being fetched is the browser's failurenym,
        # not the site's: that one download goes out again cookie-only.
        seen = []

        class Cutting(FakePublisher):
            def get_attachment(self, doi):
                seen.append(self.session)
                if self.session is not None:
                    raise Unavailable("the session browser failed during the download")
                return b"%PDF-1.4\n%fake\n"

        self.deps.publishers = {"wiley": Cutting(self.session)}
        # the hop start and the seed's download both see it alive; then it is gone
        answers = iter([True, True, False, False])
        self.session.alive = lambda: next(answers, False)
        survey.run_loop(self.project, self.deps)
        self.assertEqual(self.seen_for("10.1002/x")["download"], "cookie")
        self.assertEqual(len(seen), 3)  # the seed, then its retry, then the candidate
        self.assertIsNone(seen[1])

    def test_an_interrupted_hop_is_checkpointed_so_resume_does_not_redo_it(self):
        # Ctrl-C ends the hop at the next boundary, and the rows that hop already took are in
        # the checkpoint: the resumed run must neither re-expand nor re-download them.
        asked = []
        real = self.backend.references

        def interrupt_once(key, limit=5):
            asked.append(key)
            out = real(key, limit)
            if len(asked) == 1:
                os.kill(os.getpid(), signal.SIGINT)
            return out

        self.backend.references = interrupt_once
        self.assertEqual(survey.run_loop(self.project, self.deps)["stop"], "interrupted")
        hop_end = [e for e in project.read_trace(self.project) if e["event"] == "hop_end"]
        self.assertEqual(hop_end[0]["expanded"], ["doi:10.1002/seed"])

        survey.run_loop(self.project, self.deps)
        self.assertNotIn("10.1002/seed", asked[1:], "the interrupted expansion was re-expanded")
        copies = [r for r in project.read_seen(self.project)
                  if r.get("doi") == "10.1002/x" and r.get("download") == "ok"]
        self.assertEqual(len(copies), 1, "the interrupted hop's download was paid for twice")

    def test_a_seed_the_graph_cannot_resolve_ends_its_row_not_the_run(self):
        # One unexpandable id (an arXiv key sent to an OpenAlex-only walk, a deleted work) must
        # cost that row, not the remaining frontier; the trace has to say which row it was.
        # A 200 without a JSON body arrives as JSONDecodeError (a ValueError): an interstitial
        # is routine in a long walk. A 200 with no body at all arrives as KeyError.
        real = self.backend.references
        for exc in (NotFound("[openalex] HTTP 404: Not Found"), KeyError("not found: 0702012"),
                    ValueError("Expecting value: line 1 column 1 (char 0)")):
            with self.subTest(exc=type(exc).__name__):
                for stray in list(self.project.root.glob("seen.jsonl")):
                    stray.unlink()
                soup = self.project.root / "trace.jsonl"
                if soup.exists():
                    soup.unlink()
                project.append_seen(self.project, {"identity": "doi:10.1002/seed", "admit": True,
                                                   "reason": "seed", "seed": "flag",
                                                   "key": "10.1002/seed", "score": 0.0, "parts": {}})

                def references(key, limit=5, exc=exc):
                    if key == "10.1002/seed":
                        raise exc
                    return real(key, limit)

                self.backend.references = references
                self.assertEqual(survey.run_loop(self.project, self.deps)["stop"], "saturated")
                trace = project.read_trace(self.project)
                hops = [e for e in trace if e["event"] == "hop_end"]
                self.assertEqual(len(hops), 2)  # the row got its retry and no more
                self.assertEqual(hops[0]["expanded"], [])  # a failed hop claims nothing
                self.assertEqual(hops[1]["abandoned"], ["doi:10.1002/seed"])  # written off
                errors = [e for e in trace if e["event"] == "error"]
                self.assertEqual(errors[0]["identity"], "doi:10.1002/seed")

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
        self.assertEqual(sorted(f.name for f in files),
                         ["10.1002_seed.pdf", "10.1002_x.pdf"])  # the seed copies too
        self.assertTrue(all(f.read_bytes().startswith(b"%PDF") for f in files))

    def test_the_seed_is_downloaded_like_any_admitted_work(self):
        # A seed is admitted without ever being scored, so the only path that used to download
        # was the new-candidate one: the seed kept no copy at all, and the survey left without
        # the work it was started from.
        survey.run_loop(self.project, self.deps)
        self.assertEqual(self.seen_for("10.1002/seed")["download"], "ok")
        self.assertTrue((self.project.root / "corpus" / "10.1002_seed.pdf").exists())

    def test_a_resume_does_not_download_the_seed_again(self):
        survey.run_loop(self.project, self.deps)
        asked = []
        self.deps.resolve = lambda doi: asked.append(doi) or {"has_fulltext": False}
        survey.run_loop(self.project, self.deps)
        self.assertNotIn("10.1002/seed", asked)

    def test_a_work_admitted_in_a_hop_is_expanded_by_the_next(self):
        survey.run_loop(self.project, self.deps)
        self.assertEqual(self.backend.references_calls, 2)

    def test_a_candidate_without_a_doi_is_recorded_but_not_downloaded(self):
        self.backend._candidate = lambda: AcademicItem(
            source="openalex", key="W9", doi="", title="No DOI", year="2020",
            cited_by_count=40, extra={"openalex_type": "article", "referenced_works": []})
        calls = []
        self.deps.resolve = lambda doi: calls.append(doi) or {"has_fulltext": True}
        survey.run_loop(self.project, self.deps)
        rows = [r for r in project.read_seen(self.project)
                if r.get("key") == "W9" and r.get("admit")]
        self.assertTrue(rows)
        self.assertEqual(rows[-1]["download"], "no-doi")
        self.assertEqual(calls, ["10.1002/seed"])  # the seed, not the DOI-less candidate
        self.assertTrue([p for p in (self.project.root / "corpus").iterdir()
                         if p.suffix == ".json"])

    def test_a_second_sigint_puts_the_default_handler_back(self):
        # The first Ctrl-C must end the hop cleanly; the second must be able to abort a hung
        # call, so one strike is not allowed to swallow every later one.
        seen = []

        def sentinel(*_args):
            pass

        before = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, sentinel)
        self.addCleanup(signal.signal, signal.SIGINT, before)

        def resolve(doi):
            os.kill(os.getpid(), signal.SIGINT)
            seen.append(signal.getsignal(signal.SIGINT) is sentinel)
            return {"has_fulltext": False}

        self.deps.resolve = resolve
        survey.run_loop(self.project, self.deps)
        self.assertEqual(seen, [True, True])  # the seed's download and the candidate's

    def test_a_sigterm_ends_the_hop_and_leaves_no_lock(self):
        # A job sent SIGTERM has no terminal to press Ctrl-C at: unhandled, its default action
        # kills the process without running the `finally`, and the lock is left behind — the
        # next run then refuses with exit 2 until someone removes it by hand.
        strikes = []

        def resolve(doi):
            if not strikes:
                strikes.append(True)
                os.kill(os.getpid(), signal.SIGTERM)
            return {"has_fulltext": False}

        self.deps.resolve = resolve
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            code = survey.cmd_survey_run(self.cfg, self.args())
        self.assertEqual(code, 1)
        self.assertEqual(project.read_trace(self.project)[-1]["reason"], "interrupted")
        self.assertFalse((self.project.root / "$.lock").exists())

    def test_a_second_sigterm_can_still_raise(self):
        # Restoring SIG_DFL after the first strike would make the second one kill the process
        # exactly as the first would have, so the stack could never be unwound.
        seen = []

        def resolve(doi):
            os.kill(os.getpid(), signal.SIGTERM)
            seen.append(signal.getsignal(signal.SIGTERM))
            return {"has_fulltext": False}

        self.deps.resolve = resolve
        with (contextlib.redirect_stderr(io.StringIO()),
              contextlib.redirect_stdout(io.StringIO()),
              self.assertRaises(SystemExit) as caught):
            survey.cmd_survey_run(self.cfg, self.args())
        self.assertEqual(caught.exception.code, 128 + signal.SIGTERM)
        self.assertTrue(seen)
        self.assertNotEqual(seen[0], signal.SIG_DFL)
        self.assertFalse((self.project.root / "$.lock").exists())

    def _graph(self, *sources):
        self.deps.graph = list(sources)
        self.deps.by_name = {b.name: b for b in sources}

    def _hop_ends(self):
        return [e for e in project.read_trace(self.project) if e["event"] == "hop_end"]

    def _small_graph_budget(self, cap=2):
        """Shrink the per-source allowance so a test can spend it inside one hop."""
        real = effort.resolve

        def resolve(level, overrides=None):
            row = real(level, overrides)
            row["graph_cap"] = cap
            return row

        patcher = mock.patch.object(survey.effort, "resolve", resolve)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_site_budget_is_per_source_so_one_spent_site_does_not_end_the_run(self):
        # Every graph source is its own site with its own allowance. A source that answers one
        # axis and is spent must not stop a walk whose other source still has room.
        self._small_graph_budget()
        self._graph(NeighbourOnly(), FakeDiscovery("semanticscholar"))
        survey.run_loop(self.project, self.deps)
        hops = self._hop_ends()
        self.assertGreaterEqual(len(hops), 2)
        # Three axes, cap 2: the third is skipped rather than overdrawing the site.
        self.assertEqual(hops[0]["remaining"]["semanticscholar"], 0)
        self.assertEqual(hops[0]["remaining"]["neighbour"], 1)

    def test_the_run_stops_on_budget_only_when_every_source_is_spent(self):
        self._small_graph_budget()
        self._graph(NeighbourOnly(), FakeDiscovery("semanticscholar"))
        survey.run_loop(self.project, self.deps)
        stop = next(e for e in project.read_trace(self.project) if e["event"] == "stop")
        last = self._hop_ends()[-1]
        self.assertEqual(stop["reason"], "budget")
        # Every graph site is spent; the unrelated arxiv/publisher allowances are still whole.
        self.assertLessEqual(last["remaining"]["semanticscholar"], 0)
        self.assertLessEqual(last["remaining"]["neighbour"], 0)
        self.assertGreater(last["remaining"]["arxiv"], 0)

    def test_a_cached_answer_refunds_its_charge_and_never_more(self):
        # A cache hit costs the site no request, so it must hand its charge back; capping the
        # refund at `charged` keeps the counter from drifting above the cap on a hot cache.
        from litcli import cache as cache_mod
        stats = {"hits": 99, "misses": 0, "by": {"openalex": {"hits": 99, "misses": 0}}}
        with mock.patch.object(cache_mod, "stats", return_value=stats):
            survey.run_loop(self.project, self.deps)
        hop_end = [e for e in project.read_trace(self.project) if e["event"] == "hop_end"]
        self.assertTrue(hop_end)
        first = hop_end[0]
        self.assertEqual(first["refund"], first["charged"])
        self.assertEqual(first["remaining"]["openalex"], 40)  # low effort cap: 3 charged, refunded

    def test_the_same_hop_without_a_hit_keeps_the_charge(self):
        # The other half of the bracket: with no hits the counter must fall, or the test above
        # would pass just as well on code that never charges at all.
        survey.run_loop(self.project, self.deps)
        first = next(e for e in project.read_trace(self.project) if e["event"] == "hop_end")
        self.assertEqual(first["refund"], 0)
        self.assertEqual(first["remaining"]["openalex"], 40 - first["charged"])

    def test_a_cache_hit_refunds_every_charged_row_of_the_hop(self):
        # The refund used to be settled from the *last* row's charges only: a warm cache over a
        # multi-row hop refunded one row and drained the site budget on answers that cost no
        # request at all, ending the run on `budget`.
        project.append_seen(self.project, {"identity": "doi:10.1002/seed2", "admit": True,
                                           "reason": "seed", "key": "10.1002/seed2",
                                           "score": 0.0, "parts": {}, "download": "ok"})
        from litcli import cache as cache_mod
        stats = {"hits": 999, "misses": 0, "by": {"openalex": {"hits": 999, "misses": 0}}}
        with mock.patch.object(cache_mod, "stats", return_value=stats):
            survey.run_loop(self.project, self.deps)
        first = self._hop_ends()[0]
        self.assertEqual(first["charged"], 6)  # two rows, three axes each
        self.assertEqual(first["refund"], 6)
        self.assertEqual(first["remaining"]["openalex"], 40)

    def test_a_row_past_a_mid_batch_budget_stays_unexpanded(self):
        # The budget stop is only read between hops, so the tail of the batch that spent the
        # last unit used to be written down as expanded with nothing asked: a resume skipped it.
        self._small_graph_budget(cap=2)  # the first row's three axes spend both units
        project.append_seen(self.project, {"identity": "doi:10.1002/seed2", "admit": True,
                                           "reason": "seed", "key": "10.1002/seed2",
                                           "score": 0.0, "parts": {}, "download": "ok"})
        survey.run_loop(self.project, self.deps)
        self.assertEqual(self._hop_ends()[0]["expanded"], ["doi:10.1002/seed"])

    def test_a_row_no_source_can_address_is_not_retried_forever(self):
        # A Crossref reference without a DOI keeps its text as the key; no source takes that
        # spelling, so the row can never be expanded and must not be put back every hop.
        project.append_seen(self.project, {"identity": "title:A book, 1999", "admit": True,
                                           "reason": "seed", "key": "A book, 1999",
                                           "score": 0.0, "parts": {}, "download": "ok"})
        survey.run_loop(self.project, self.deps)
        first = self._hop_ends()[0]
        self.assertIn("title:A book, 1999", first["expanded"])

    def test_a_bot_wall_is_retried_like_a_throttle(self):
        # A challenge page is the failure most likely to clear on its own; it used to be
        # treated as "not this row" and written off after one retry.
        from litcli.errors import BotWall

        self.backend.references = raising(BotWall("[openalex] challenge page"))
        survey.run_loop(self.project, self.deps)
        self.assertEqual([e["abandoned"] for e in self._hop_ends()], [[] for _ in self._hop_ends()])

    def test_the_row_is_charged_what_the_expansion_actually_spent(self):
        # One `references` call is a handful of batch requests, not one: charging per axis
        # alone would let a 40-request level walk a hundred works.
        from litcli import cache as cache_mod
        spent = {"misses": 0}
        real = self.backend.references

        def references(key, limit=5):
            spent["misses"] += 12
            return real(key, limit)

        self.backend.references = references
        with mock.patch.object(
                cache_mod, "stats",
                lambda: {"hits": 0, "misses": spent["misses"],
                         "by": {"openalex": {"hits": 0, "misses": spent["misses"]}}}):
            survey.run_loop(self.project, self.deps)
        first = next(e for e in project.read_trace(self.project) if e["event"] == "hop_end")
        self.assertGreaterEqual(first["charged"], 12)
        self.assertEqual(first["remaining"]["openalex"], 40 - first["charged"])

    def test_a_corpus_file_carries_one_extension(self):
        # safe_filename(..., "") leaves a trailing dot, so appending ".pdf" to it yields
        # "10.1002_x..pdf".
        survey.run_loop(self.project, self.deps)
        names = [p.name for p in (self.project.root / "corpus").iterdir()]
        self.assertTrue(names)
        self.assertTrue(all(".." not in name for name in names), names)
