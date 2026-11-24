"""The on-disk survey project: one directory per question, append-only history.

A survey outlives the process that started it, so everything is a file under the project
root: the question as ``project.toml``, every candidate ever judged in ``seen.jsonl`` and every
step in ``trace.jsonl``. Writes go through a sibling ``.tmp`` and ``os.replace``, so an
interrupted append can never leave a half line behind — and a half line that is there anyway
is reported with its file and line number instead of being skipped.
"""
import os
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from litcli import project
from litcli.errors import BadInput


class SlugTest(unittest.TestCase):
    def test_a_question_becomes_a_short_slug(self):
        self.assertEqual(project.slugify("Vapor chamber wick boiling limit in thin cells"),
                         "Vapor_chamber_wick_boiling_limit_in")

    def test_an_empty_question_falls_back(self):
        self.assertEqual(project.slugify("  ???  "), "survey")


class CreateTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_the_project_directory_is_the_slug_under_the_root(self):
        p = project.create(self.root, "Wick boiling limit")
        self.assertEqual(p.root, self.root / "Wick_boiling_limit")

    def test_a_second_project_with_the_same_question_gets_a_suffix(self):
        first = project.create(self.root, "Wick boiling limit")
        second = project.create(self.root, "Wick boiling limit")
        self.assertEqual(second.root.name, first.root.name + "-2")
        third = project.create(self.root, "Wick boiling limit")
        self.assertEqual(third.root.name, first.root.name + "-3")

    def test_defaults_are_filled_in(self):
        p = project.create(self.root, "Wick boiling limit")
        self.assertEqual(p.threshold, 2.5)
        self.assertEqual(p.effort, "medium")
        self.assertEqual(p.status, "running")
        self.assertTrue(p.created)

    def test_the_file_is_parseable_toml(self):
        p = project.create(self.root, "Wick boiling limit")
        data = tomllib.loads((p.root / "project.toml").read_text(encoding="utf-8"))
        self.assertEqual(data["question"], "Wick boiling limit")


class RoundTripTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.p = project.create(self.root, "Wick boiling limit", seeds=["10.1016/j.ijheatmasstransfer.2020.1"],
                                type_weight={"article": 1.0}, threshold=3.0)

    def tearDown(self):
        self._tmp.cleanup()

    def test_load_returns_what_was_saved(self):
        self.p.status = "paused"
        project.save(self.p)
        again = project.load(self.p.root)
        self.assertEqual(again.status, "paused")
        self.assertEqual(again.seeds, self.p.seeds)
        self.assertEqual(again.type_weight, self.p.type_weight)
        self.assertEqual(again.threshold, 3.0)

    def test_a_record_survives_append_and_read(self):
        project.append_seen(self.p, {"key": "doi:10.1/x", "score": 2.75, "note": 'quo"te'})
        self.assertEqual(project.read_seen(self.p),
                         [{"key": "doi:10.1/x", "score": 2.75, "note": 'quo"te'}])

    def test_a_trace_record_survives_append_and_read(self):
        project.append_trace(self.p, {"step": 1, "action": "expand"})
        self.assertEqual(project.read_trace(self.p), [{"step": 1, "action": "expand"}])

    def test_reads_of_a_fresh_project_are_empty(self):
        self.assertEqual(project.read_seen(self.p), [])
        self.assertEqual(project.read_trace(self.p), [])

    def test_corpus_dir_is_created(self):
        d = project.corpus_dir(self.p)
        self.assertTrue(d.is_dir())


class BrokenFileTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.p = project.create(self.root, "Wick boiling limit")

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_half_line_names_the_file_and_its_line(self):
        (self.p.root / "seen.jsonl").write_text('{"key": "a"}\n{"key": "b",\n', encoding="utf-8")
        with self.assertRaises(BadInput) as caught:
            project.load(self.p.root)
        self.assertIn("seen.jsonl", str(caught.exception))
        self.assertIn("2", str(caught.exception))

    def test_broken_toml_names_the_file_and_its_line(self):
        (self.p.root / "project.toml").write_text("question = \n", encoding="utf-8")
        with self.assertRaises(BadInput) as caught:
            project.load(self.p.root)
        self.assertIn("project.toml", str(caught.exception))

    def test_a_missing_project_is_a_bad_input(self):
        with self.assertRaises(BadInput):
            project.load(self.root / "nope")

    def test_a_missing_question_is_a_bad_input(self):
        (self.p.root / "project.toml").write_text("status = 'running'\n", encoding="utf-8")
        with self.assertRaises(BadInput):
            project.load(self.p.root)
