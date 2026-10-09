"""data add 摄入流程桩测:mock metadata + backend,断言 --pdf 启发式候选 / 查重门 / --force / --csl。"""
import argparse
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from litcli.cli import cmd_data_add


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
             type="document", year="2020", doi="", url="", abstract="", force=False)
    d.update(over)
    return argparse.Namespace(**d)


class DataAddStubTest(unittest.TestCase):
    def test_pdf_heuristic_shows_candidate_then_writes(self):
        fb = _FakeBackend()
        with mock.patch("litcli.cli.metadata.extract_metadata", return_value=_item(title="Deep learning", doi="10.1/x")), \
             mock.patch("litcli.cli.get_backend", return_value=fb):
            rc = cmd_data_add({}, _args(pdf="/tmp/a.pdf"))
        self.assertEqual(rc, 0)
        self.assertEqual(len(fb.added), 1)
        self.assertEqual(fb.added[0].title, "Deep learning")

    def test_duplicate_doi_rejected_without_force(self):
        fb = _FakeBackend(existing=[_item(doi="10.1/x", title="Old")])
        with mock.patch("litcli.cli.metadata.extract_metadata", return_value=_item(title="New", doi="10.1/x")), \
             mock.patch("litcli.cli.get_backend", return_value=fb):
            rc = cmd_data_add({}, _args(pdf="/tmp/a.pdf"))
        self.assertEqual(rc, 2)
        self.assertEqual(len(fb.added), 0)

    def test_force_overrides_duplicate(self):
        fb = _FakeBackend(existing=[_item(doi="10.1/x", title="Old")])
        with mock.patch("litcli.cli.metadata.extract_metadata", return_value=_item(title="New", doi="10.1/x")), \
             mock.patch("litcli.cli.get_backend", return_value=fb):
            rc = cmd_data_add({}, _args(pdf="/tmp/a.pdf", force=True))
        self.assertEqual(rc, 0)
        self.assertEqual(len(fb.added), 1)

    def test_csl_input_uses_from_csl(self):
        fb = _FakeBackend()
        csl = '{"title":"CSL Title","author":[{"family":"Doe","given":"J"}],"issued":{"date-parts":[[2020]]}}'
        with mock.patch("litcli.cli.get_backend", return_value=fb), \
             mock.patch("builtins.open", mock.mock_open(read_data=csl)) as mo:
            rc = cmd_data_add({}, _args(csl="/tmp/x.json", doi="10.9/z", title="Ignored"))
        self.assertEqual(rc, 0)
        self.assertEqual(len(fb.added), 1)
        self.assertEqual(fb.added[0].title, "CSL Title")

    def test_csl_missing_authors_hits_half_gate(self):
        fb = _FakeBackend()
        with mock.patch("litcli.cli.get_backend", return_value=fb), \
             mock.patch("builtins.open", mock.mock_open(read_data='{"title":"NoAuthor"}')) as mo:
            rc = cmd_data_add({}, _args(csl="/tmp/x.json"))
        self.assertEqual(rc, 2)  # 半残门拦截(无作者无年份)
        self.assertEqual(len(fb.added), 0)


if __name__ == "__main__":
    unittest.main()
