"""data add ingestion-flow stub tests: mock metadata + backend, assert --pdf heuristic candidate /
dedup gate / --force / --csl."""
import argparse
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from litcli.commands.data import cmd_data_add


class _FakeBackend:
    def __init__(self, existing=None):
        self.name = "zotero"
        self.added = []
        self._existing = existing or []

    def search(self, query, limit=10):
        return self._existing

    def add(self, item):
        self.added.append(item)
        return "NEWKEY"


def _item(title="T", creators=None, year="2020", doi=""):
    from litcli.models import AcademicItem
    return AcademicItem(source="zotero", key="", title=title,
                        creators=creators or [{"family": "A", "given": "B"}],
                        year=year, doi=doi)


def _args(**over):
    d = dict(backend="zotero", pdf=None, csl=None, title="T", creator=[],
             type="document", year="2020", doi="", url="", abstract="", force=False, yes=False)
    d.update(over)
    return argparse.Namespace(**d)


class DataAddStubTest(unittest.TestCase):
    def test_pdf_heuristic_shows_candidate_then_writes(self):
        fb = _FakeBackend()
        with mock.patch("litcli.metadata.extract_metadata", return_value=_item(title="Deep learning", doi="10.1/x")), \
             mock.patch("litcli.commands.data.get_backend", return_value=fb):
            rc = cmd_data_add({}, _args(pdf="/tmp/a.pdf", yes=True))
        self.assertEqual(rc, 0)
        self.assertEqual(len(fb.added), 1)
        self.assertEqual(fb.added[0].title, "Deep learning")

    def test_pdf_heuristic_cancel_does_not_write(self):
        # Without --yes/--force the confirm prompt must be answered; a negative answer cancels the write.
        fb = _FakeBackend()
        with mock.patch("litcli.metadata.extract_metadata", return_value=_item(title="Deep learning", doi="10.1/x")), \
             mock.patch("litcli.commands.data.get_backend", return_value=fb), \
             mock.patch("builtins.input", return_value="n"), \
             mock.patch("litcli.commands.data.sys.stdin.isatty", return_value=True):
            rc = cmd_data_add({}, _args(pdf="/tmp/a.pdf"))
        self.assertEqual(rc, 2)
        self.assertEqual(len(fb.added), 0)

    def test_no_terminal_refuses_the_review_instead_of_cancelling(self):
        # Piped stdin used to hit EOF and read as "user cancelled"; say what to do instead.
        from litcli.errors import BadInput

        fb = _FakeBackend()
        with mock.patch("litcli.metadata.extract_metadata", return_value=_item(title="T", doi="10.1/x")), \
             mock.patch("litcli.commands.data.get_backend", return_value=fb), \
             mock.patch("litcli.commands.data.sys.stdin.isatty", return_value=False), \
             self.assertRaises(BadInput) as caught:
            cmd_data_add({}, _args(pdf="/tmp/a.pdf"))
        self.assertIn("--yes", str(caught.exception))
        self.assertEqual(fb.added, [])

    def test_duplicate_doi_rejected_without_force(self):
        fb = _FakeBackend(existing=[_item(doi="10.1/x", title="Old")])
        with mock.patch("litcli.metadata.extract_metadata", return_value=_item(title="New", doi="10.1/x")), \
             mock.patch("litcli.commands.data.get_backend", return_value=fb):
            rc = cmd_data_add({}, _args(pdf="/tmp/a.pdf"))
        self.assertEqual(rc, 2)
        self.assertEqual(len(fb.added), 0)

    def test_force_overrides_duplicate(self):
        fb = _FakeBackend(existing=[_item(doi="10.1/x", title="Old")])
        with mock.patch("litcli.metadata.extract_metadata", return_value=_item(title="New", doi="10.1/x")), \
             mock.patch("litcli.commands.data.get_backend", return_value=fb):
            rc = cmd_data_add({}, _args(pdf="/tmp/a.pdf", force=True))
        self.assertEqual(rc, 0)
        self.assertEqual(len(fb.added), 1)

    def test_csl_input_uses_from_csl(self):
        fb = _FakeBackend()
        csl = '{"title":"CSL Title","author":[{"family":"Doe","given":"J"}],"issued":{"date-parts":[[2020]]}}'
        with mock.patch("litcli.commands.data.get_backend", return_value=fb), \
             mock.patch("builtins.open", mock.mock_open(read_data=csl)):
            rc = cmd_data_add({}, _args(csl="/tmp/x.json", doi="10.9/z", title="Ignored"))
        self.assertEqual(rc, 0)
        self.assertEqual(len(fb.added), 1)
        self.assertEqual(fb.added[0].title, "CSL Title")

    @staticmethod
    def _csl(*records):
        return json.dumps(list(records))

    def test_csl_array_from_an_export_adds_every_record(self):
        # ``export --format csljson`` emits a JSON array; ``data add --csl`` must take all of it.
        fb = _FakeBackend()
        payload = self._csl(
            {"title": "First", "author": [{"family": "A"}], "issued": {"date-parts": [[2020]]}},
            {"title": "Second", "author": [{"family": "B"}], "issued": {"date-parts": [[2021]]}})
        with mock.patch("litcli.commands.data.get_backend", return_value=fb), \
             mock.patch("builtins.open", mock.mock_open(read_data=payload)):
            rc = cmd_data_add({}, _args(csl="/tmp/batch.json"))
        self.assertEqual(rc, 0)
        self.assertEqual([i.title for i in fb.added], ["First", "Second"])

    def test_csl_array_gates_each_record_separately(self):
        fb = _FakeBackend(existing=[_item(doi="10.1/dup", title="Old")])
        payload = self._csl(
            {"title": "Dupe", "DOI": "10.1/dup", "author": [{"family": "A"}],
             "issued": {"date-parts": [[2020]]}},
            {"title": "Fresh", "author": [{"family": "B"}], "issued": {"date-parts": [[2021]]}})
        with mock.patch("litcli.commands.data.get_backend", return_value=fb), \
             mock.patch("builtins.open", mock.mock_open(read_data=payload)):
            rc = cmd_data_add({}, _args(csl="/tmp/batch.json"))
        self.assertEqual(rc, 2)  # the batch was partly gated
        self.assertEqual([i.title for i in fb.added], ["Fresh"])

    def test_an_empty_csl_array_is_reported(self):
        fb = _FakeBackend()
        with mock.patch("litcli.commands.data.get_backend", return_value=fb), \
             mock.patch("builtins.open", mock.mock_open(read_data="[]")):
            rc = cmd_data_add({}, _args(csl="/tmp/empty.json"))
        self.assertEqual(rc, 2)
        self.assertEqual(fb.added, [])

    def test_csl_missing_authors_hits_half_gate(self):
        fb = _FakeBackend()
        with mock.patch("litcli.commands.data.get_backend", return_value=fb), \
             mock.patch("builtins.open", mock.mock_open(read_data='{"title":"NoAuthor"}')):
            rc = cmd_data_add({}, _args(csl="/tmp/x.json"))
        self.assertEqual(rc, 2)  # semi-import gate rejection (no authors, no year)
        self.assertEqual(len(fb.added), 0)


if __name__ == "__main__":
    unittest.main()
