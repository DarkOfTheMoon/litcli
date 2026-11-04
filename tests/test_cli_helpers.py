"""CLI stub tests: key normalization, the argparse surface, rendering, and the backend-failure paths.

No network; only pure functions and the argparse surface (no backend instantiation, no requests).
Backend helpers are patched on the command module that uses them, not on ``litcli.cli``.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from litcli.backends.base import Backend, NotSupportedError
from litcli.commands.keys import arxiv_id_from_key, normalize_export_key, normalize_related_key
from litcli.commands.parser import build_parser
from litcli.commands.render import print_items
from litcli.models import AcademicItem


class ArxivKeyNormalizeTest(unittest.TestCase):
    def test_arxiv_id_from_prefixed_forms(self):
        self.assertEqual(arxiv_id_from_key("arxiv/1806.09180"), "1806.09180")
        self.assertEqual(arxiv_id_from_key("arXiv:1806.09180"), "1806.09180")
        self.assertEqual(arxiv_id_from_key("arxiv/1806.09180v1"), "1806.09180")

    def test_arxiv_id_from_non_arxiv(self):
        self.assertIsNone(arxiv_id_from_key("10.1/abc"))
        self.assertIsNone(arxiv_id_from_key("10.48550/arxiv.1806.09180"))
        self.assertIsNone(arxiv_id_from_key(""))

    def test_normalize_for_arxiv_backend_uses_bare_id(self):
        self.assertEqual(normalize_export_key("arxiv/1806.09180", "arxiv"), "1806.09180")

    def test_normalize_for_other_backend_uses_doi(self):
        # Non-arxiv backends (openalex/crossref) need DOI form to resolve, otherwise queried as an OpenAlex id -> 404
        self.assertEqual(normalize_export_key("arxiv/1806.09180", "openalex"),
                         "10.48550/arxiv.1806.09180")

    def test_non_arxiv_key_passthrough(self):
        self.assertEqual(normalize_export_key("10.1/abc", "crossref"), "10.1/abc")


class RelatedKeyNormalizeTest(unittest.TestCase):
    def test_strips_source_prefix(self):
        self.assertEqual(normalize_related_key("openalex/10.1016/j.cma.2004.11.019"),
                         "10.1016/j.cma.2004.11.019")
        self.assertEqual(normalize_related_key("zotero/AB12"), "AB12")
        self.assertEqual(normalize_related_key("scihub/10.1016/j.cma.2004.11.019"),
                         "10.1016/j.cma.2004.11.019")

    def test_arxiv_prefix_to_doi(self):
        self.assertEqual(normalize_related_key("arxiv/1806.09180"), "10.48550/arxiv.1806.09180")

    def test_openalex_url_and_doi_passthrough(self):
        # extra["openalex_id"] form (CLI-provided) is delivered as-is; bare DOI is also left untouched
        self.assertEqual(normalize_related_key("https://openalex.org/W2919115771"),
                         "https://openalex.org/W2919115771")
        self.assertEqual(normalize_related_key("10.48550/arxiv.1806.09180"),
                         "10.48550/arxiv.1806.09180")


class BackendsParserTest(unittest.TestCase):
    def test_backends_accepts_list_and_status(self):
        p = build_parser()
        for cmd in ("list", "status"):
            a = p.parse_args(["backends", cmd])
            self.assertEqual(a.backends_cmd, cmd)
        a = p.parse_args(["backends"])
        self.assertIsNone(a.backends_cmd)
        a = p.parse_args(["backends", "authorize"])
        self.assertEqual(a.backends_cmd, "authorize")

    def test_search_accepts_type_filter(self):
        a = build_parser().parse_args(["search", "q", "--type", "article"])
        self.assertEqual(a.type, "article")

    def test_search_type_defaults_none(self):
        a = build_parser().parse_args(["search", "q"])
        self.assertIsNone(a.type)

    def test_edge_subcommands_share_options(self):
        p = build_parser()
        for cmd in ("refs", "citing", "related"):
            a = p.parse_args([cmd, "10.1/x", "--limit", "7", "--sort", "cited",
                              "--backend", "openalex", "--format", "json"])
            self.assertEqual(a.cmd, cmd)
            self.assertEqual(a.limit, 7)
            self.assertEqual(a.sort, "cited")
            self.assertEqual(a.backend, "openalex")
            self.assertEqual(a.format, "json")


class PrintItemsTest(unittest.TestCase):
    def test_json_empty_prints_empty_list(self):
        # Empty json/csl results must emit a machine-readable ``[]``, not a silent stdout.
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = print_items([], "json", False, "[no related]")
        self.assertEqual(rc, 0)
        self.assertEqual(buf.getvalue().strip(), "[]")


class FetchBookTest(unittest.TestCase):
    """fetch book: source labelling and the no-file backend path."""

    class OpenLibrary:
        name = "openlibrary"
        capabilities = {"read"}

        def search(self, q, limit=10, filters=None):
            return [AcademicItem(source="openlibrary", key="/books/OL1M", title="T", item_type="book")]

        def get(self, key):
            return AcademicItem(source="openlibrary", key=key, title="T", item_type="book",
                                url="https://openlibrary.org/books/OL1M")

        def get_attachment(self, key):
            raise NotSupportedError("get_attachment", self)

    class Shadow:
        name = "books"  # a zlibrary backend configured under a non-default name
        is_shadow = True

        def search(self, q, limit=10, filters=None):
            return [AcademicItem(source="books", key="book/abc", title="T", item_type="book")]

        def get(self, key):
            return AcademicItem(source="books", key=key, title="T", item_type="book", url="https://z.test/x")

    def _run(self, argv, backend):
        import contextlib
        import io
        from unittest import mock
        import litcli.cli as cli

        buf = io.StringIO()
        with mock.patch("litcli.commands.fetch.get_backend", return_value=backend), \
             contextlib.redirect_stdout(buf):
            rc = cli.main(argv)
        return rc, buf.getvalue()

    def test_download_on_a_metadata_only_backend_reports_the_page(self):
        rc, out = self._run(["fetch", "book", "x", "--backend", "openlibrary", "--download"],
                            self.OpenLibrary())
        self.assertEqual(rc, 0)
        self.assertIn("has no downloadable file", out)

    class MetadataOnly(Backend):
        name = "openlibrary"

        def search(self, q, limit=10, filters=None):
            return [AcademicItem(source="openlibrary", key="/books/OL1M", title="T", item_type="book")]

        def get(self, key):
            return AcademicItem(source="openlibrary", key=key, title="T", item_type="book",
                                url="https://openlibrary.org/books/OL1M")

    def test_metadata_only_backend_has_no_download_hint(self):
        rc, out = self._run(["fetch", "book", "x", "--backend", "openlibrary"], self.MetadataOnly())
        self.assertIn("metadata only, no file", out)
        self.assertNotIn("Add --download", out)

    def test_shadow_label_survives_a_renamed_backend(self):
        rc, out = self._run(["fetch", "book", "x", "--backend", "books"], self.Shadow())
        self.assertIn("shadow library", out)
        self.assertNotIn("record (open access)", out)


class MainCatchTest(unittest.TestCase):
    def test_config_syntax_error_surfaces_as_error(self):
        # A malformed config.toml (TOMLDecodeError) must surface as [error] exit 2, never a traceback.
        import contextlib, io, tomllib
        from unittest import mock
        import litcli.cli as cli

        with mock.patch("litcli.cli.load_config", side_effect=tomllib.TOMLDecodeError("bad config")), \
             mock.patch("litcli.cli._apply_proxy"):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cli.main(["search", "x"])
        self.assertEqual(rc, 2)
        self.assertIn("[error]", buf.getvalue())
        self.assertNotIn("Traceback", buf.getvalue())

    def test_search_all_skips_valueerror_backend(self):
        # A single backend returning a non-JSON body (ValueError) must be skipped, not crash --all.
        import argparse, contextlib, io
        from unittest import mock
        from litcli.commands.search import cmd_search
        from litcli.models import AcademicItem

        class _Raises:
            def __init__(self, name="bad"):
                self.name = name
            def search(self, *a, **k):
                raise ValueError("non-JSON body")

        class _Ok:
            def __init__(self, name="ok"):
                self.name = name
            def search(self, *a, **k):
                return [AcademicItem(source="ok", key="k", title="Good", year="2020")]

        args = argparse.Namespace(all=True, query="x", limit=10, format="table",
                                  no_cite=False, type=None, sort=None, backend="zotero",
                                  title=None, author=None, year=None, container=None)
        out = io.StringIO()
        err = io.StringIO()
        with mock.patch("litcli.commands.search.get_all_backends", return_value={"bad": _Raises(), "ok": _Ok()}):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = cmd_search({}, args)
        self.assertEqual(rc, 0)
        self.assertIn("[skipped]", err.getvalue())
        self.assertIn("Good", out.getvalue())


if __name__ == "__main__":
    unittest.main()
