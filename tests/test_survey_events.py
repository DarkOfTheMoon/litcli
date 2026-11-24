"""The survey trace is line JSON with a fixed event vocabulary, and renders as hop tables.

The trace is both the loop's checkpoint and the human record, so it is one JSON object per
line with ``event`` first — greppable, appendable, and parseable by a reader that does not
know the rest of the schema. Anything else in a line is event-specific.

The markdown view is what the user actually reads: one table per hop, with the download
verdict visible in the row, because "admitted but no copy" is a fact the survey must show
rather than hide.
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from litcli import survey_events
from litcli.errors import BadInput

EVENTS = ("start", "candidate", "admit", "download", "error", "hop_end", "stop")


class LineTest(unittest.TestCase):
    def test_the_event_leads_the_object(self):
        self.assertTrue(survey_events.line("start", question="q").startswith('{"event": '))

    def test_a_line_parses_back(self):
        self.assertEqual(json.loads(survey_events.line("stop", reason="saturated")),
                         {"event": "stop", "reason": "saturated"})

    def test_every_named_event_is_known(self):
        self.assertEqual(survey_events.EVENTS, EVENTS)
        for event in EVENTS:
            survey_events.line(event)

    def test_an_unknown_event_is_a_bad_input(self):
        with self.assertRaises(BadInput):
            survey_events.line("nope")

    def test_unicode_survives_unscaped(self):
        self.assertIn("沸腾", survey_events.line("candidate", title="沸腾极限"))


HOP = [
    {"event": "start", "question": "wick boiling"},
    {"event": "candidate", "hop": 1, "key": "10.1/a", "score": 3.1, "year": "2020",
     "reason": "score 3.10 >= 2.50", "title": "First"},
    {"event": "download", "hop": 1, "key": "10.1/a", "status": "unavailable"},
    {"event": "candidate", "hop": 1, "key": "10.1/b", "score": 2.6, "year": "2021",
     "reason": "score 2.60 >= 2.50", "title": "Second"},
    {"event": "download", "hop": 1, "key": "10.1/b", "status": "ok"},
    {"event": "hop_end", "hop": 1, "new": 2},
    {"event": "candidate", "hop": 2, "key": "10.1/c", "score": 2.7, "year": "2022",
     "reason": "score 2.70 >= 2.50", "title": "Third"},
    {"event": "hop_end", "hop": 2, "new": 1},
    {"event": "stop", "reason": "max_hops"},
]


class RenderTest(unittest.TestCase):
    def setUp(self):
        self.md = survey_events.render_md(HOP)

    def test_each_hop_gets_its_own_table(self):
        self.assertEqual(self.md.count("| score |"), 2)

    def test_the_columns_are_the_fixed_ones(self):
        self.assertIn("| score | year | key | reason | title |", self.md)

    def test_rows_stay_with_their_hop(self):
        first, second = self.md.split("Hop 2")[0], self.md.split("Hop 2")[1]
        self.assertIn("10.1/a", first)
        self.assertNotIn("10.1/c", first)
        self.assertIn("10.1/c", second)

    def test_an_unavailable_download_is_marked_in_the_row(self):
        row = next(r for r in self.md.splitlines() if "10.1/a" in r)
        self.assertIn("unavailable", row)

    def test_a_served_download_is_not_marked(self):
        row = next(r for r in self.md.splitlines() if "10.1/b" in r)
        self.assertNotIn("unavailable", row)

    def test_an_empty_trace_renders_empty(self):
        self.assertEqual(survey_events.render_md([]), "")

    def test_a_pipe_inside_a_title_does_not_break_the_table(self):
        md = survey_events.render_md([
            {"event": "candidate", "hop": 1, "key": "k", "score": 3, "year": "2020",
             "reason": "r", "title": "a | b"},
            {"event": "hop_end", "hop": 1, "new": 1},
        ])
        row = next(r for r in md.splitlines() if "| k |" in r)
        self.assertIn("a \\| b", row)


class ErrorNoteTest(unittest.TestCase):
    def test_an_expansion_failure_shows_under_its_hop(self):
        # A seed the graph could not expand must be visible in the rendered view, not only in
        # the JSONL: "hop produced nothing" and "hop produced nothing because of a 404" differ.
        events = [{"event": "start", "hop": 0},
                  {"event": "error", "hop": 1, "identity": "doi:10.1/x",
                   "reason": "[openalex] HTTP 404: Not Found"},
                  {"event": "hop_end", "hop": 1}]
        rendered = survey_events.render_md(events)
        self.assertIn("### Hop 1", rendered)
        self.assertIn("`doi:10.1/x` — [openalex] HTTP 404: Not Found", rendered)

    def test_an_error_alone_still_makes_a_hop(self):
        rendered = survey_events.render_md([{"event": "error", "hop": 3, "identity": "W1",
                                             "reason": "boom"}])
        self.assertIn("### Hop 3", rendered)
