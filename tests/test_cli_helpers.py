"""CLI stub tests: key normalization, the argparse surface, rendering, and the backend-failure paths.

No network; only pure functions and the argparse surface (no backend instantiation, no requests).
Backend helpers are patched on the command module that uses them, not on ``litcli.cli``.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from typing import ClassVar

from litcli.backends.base import Backend, NotSupportedError
from litcli.commands.keys import arxiv_id_from_key, normalize_export_key, normalize_related_key
from litcli.commands.parser import build_parser
from litcli.commands.render import present, print_items
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


class ResolveBackendTest(unittest.TestCase):
    """A ``source/key`` prefix picks the source, an explicit ``--backend`` overrides it, and a bare
    key falls back — a key that only looks prefixed (``10.1016/j...``) keeps its slash."""

    def _resolve(self, key, explicit=None, fallback="zotero"):
        from unittest import mock

        from litcli.commands.keys import resolve_backend

        class _B:
            def __init__(self, name):
                self.name = name

        def fake_get_backend(name, _cfg):
            return _B(name)

        with mock.patch("litcli.commands.keys.get_backend", side_effect=fake_get_backend):
            backend, bare = resolve_backend({}, explicit, key, fallback)
        return backend.name, bare

    def test_the_prefix_selects_the_source(self):
        self.assertEqual(self._resolve("openalex/W123"), ("openalex", "W123"))

    def test_the_zotero_prefix_resolves_to_the_local_store(self):
        self.assertEqual(self._resolve("zotero/AB12"), ("zotero_local", "AB12"))

    def test_an_explicit_backend_overrides_the_prefix(self):
        self.assertEqual(self._resolve("openalex/W123", explicit="crossref"),
                         ("crossref", "openalex/W123"))

    def test_an_explicit_backend_matching_the_prefix_keeps_the_bare_key(self):
        self.assertEqual(self._resolve("openalex/W123", explicit="openalex"), ("openalex", "W123"))

    def test_a_bare_key_falls_back(self):
        self.assertEqual(self._resolve("AB12", fallback="zotero"), ("zotero", "AB12"))

    def test_a_doi_is_not_read_as_a_prefix(self):
        self.assertEqual(self._resolve("10.1016/j.cma.2004.11.019", fallback="zotero"),
                         ("zotero", "10.1016/j.cma.2004.11.019"))


class PreflightTest(unittest.TestCase):
    """A local store that is not running fails before the prompt, with the one hint that helps."""

    def _backend(self):
        from litcli.backends.zotero_local import ZoteroLocalBackend
        return ZoteroLocalBackend()

    def test_a_closed_port_raises_with_the_start_hint(self):
        from unittest import mock

        from litcli.errors import Unavailable

        with mock.patch("litcli.backends.zotero_local.socket.create_connection",
                        side_effect=ConnectionRefusedError("refused")), self.assertRaises(Unavailable) as caught:
            self._backend().ensure_running()
        self.assertIn("Zotero", caught.exception.hint)

    def test_a_listening_port_passes(self):
        from unittest import mock

        with mock.patch("litcli.backends.zotero_local.socket.create_connection"):
            self._backend().ensure_running()  # no raise

    def test_writes_preflight_and_read_only_backends_do_not(self):
        import argparse
        from unittest import mock

        from litcli.commands import data

        fake = mock.Mock(name="zotero")  # Mock.name is not the string; keep it explicit
        fake.name = "zotero"
        with mock.patch.object(data, "resolve_backend", return_value=(fake, "K1")):
            data.cmd_data_del({}, argparse.Namespace(backend=None, key="K1"))
        fake.ensure_running.assert_called_once()
        fake.delete.assert_called_once_with("K1")

        plain = mock.Mock(spec=[])  # a backend without ensure_running
        data._writable(plain)  # must not raise


class TableRowTest(unittest.TestCase):
    """The table must stay judgeable: whole title, container, DOI, and no author flood."""

    def _item(self, **kw):
        base = dict(source="openalex", key="10.1/x", title="T", year="2020")
        base.update(kw)
        return AcademicItem(**base)

    def test_a_long_title_is_not_truncated(self):
        self.assertIn("A" * 120, present(self._item(title="A" * 120), no_cite=True))

    def test_container_and_doi_are_shown(self):
        row = present(self._item(container_title="Nature", doi="10.1/x"), no_cite=True)
        self.assertIn("Nature", row)
        self.assertIn("doi:10.1/x", row)

    def test_the_citation_badge_names_its_source(self):
        row = present(self._item(access="oa", cited_by_count=5,
                                 extra={"cited_by_source": {"openalex": 5}}), no_cite=True)
        self.assertIn("[cited 5 via openalex]", row)

    def test_disagreeing_sources_are_flagged(self):
        row = present(self._item(access="oa", cited_by_count=7,
                                 extra={"cited_by_source": {"openalex": 7, "crossref": 3}}),
                      no_cite=True)
        self.assertIn("(sources disagree)", row)

    def test_a_long_author_list_is_capped(self):
        creators = [{"family": f} for f in "ABCDE"]
        row = present(self._item(creators=creators), no_cite=True)
        self.assertIn("A, B, C et al.", row)
        self.assertNotIn("D", row.split("  ")[2])


class DataGetKeyTest(unittest.TestCase):
    """A key that names a DOI-less source still resolves on the DOI backends."""

    def _get(self, backend):
        import argparse
        from unittest import mock

        from litcli.commands import data

        with mock.patch.object(data, "resolve_backend", return_value=(backend, "1806.09180")) as _r, \
                mock.patch("builtins.print"):
            data.cmd_data_get({}, argparse.Namespace(backend=None, key="arxiv/1806.09180",
                                                     format="json"))
        return backend.get.call_args.args[0]

    def test_the_arxiv_id_becomes_a_doi_on_a_public_backend(self):
        from unittest import mock

        b = mock.Mock()
        b.access = "oa"
        b.name = "openalex"
        b.get.return_value = AcademicItem(source="openalex", key="k")
        self.assertEqual(self._get(b), "10.48550/arxiv.1806.09180")

    def test_the_local_store_keeps_its_own_item_key(self):
        from unittest import mock

        b = mock.Mock()
        b.access = "local"
        b.name = "zotero"
        b.get.return_value = AcademicItem(source="zotero", key="k")
        self.assertEqual(self._get(b), "1806.09180")


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
        self.assertEqual(p.parse_args(["backends", "install-browser"]).browser, "chromium")

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
    """fetch --book: source labelling and the no-file backend path.

    Labels and notes go to stderr (stdout carries only the result), so both streams are captured.
    """

    class OpenLibrary:
        name = "openlibrary"
        capabilities: ClassVar[set[str]] = {"read"}

        def search(self, q, limit=10, filters=None):
            return [AcademicItem(source="openlibrary", key="/books/OL1M", title="T", item_type="book")]

        def get(self, key):
            return AcademicItem(source="openlibrary", key=key, title="T", item_type="book",
                                url="https://openlibrary.org/books/OL1M")

        def get_attachment(self, key):
            raise NotSupportedError("get_attachment", self)

    class PublicCopy:
        name = "books"  # a zlibrary backend configured under a non-default name
        access = "public"

        def search(self, q, limit=10, filters=None):
            return [AcademicItem(source="books", key="book/abc", title="T", item_type="book")]

        def get(self, key):
            return AcademicItem(source="books", key=key, title="T", item_type="book", url="https://z.test/x")

    def _run(self, argv, backend):
        import contextlib
        import io
        from unittest import mock

        import litcli.cli as cli

        buf, err = io.StringIO(), io.StringIO()
        with mock.patch("litcli.commands.fetch.get_backend", return_value=backend), \
                contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            rc = cli.main(argv)
        return rc, buf.getvalue() + err.getvalue()

    def test_download_on_a_metadata_only_backend_reports_the_page(self):
        rc, out = self._run(["fetch", "--book", "x", "--backend", "openlibrary"], self.OpenLibrary())
        self.assertEqual(rc, 1)  # the record resolved, but no bytes exist to save
        self.assertIn("has no downloadable file", out)

    class MetadataOnly(Backend):
        name = "openlibrary"

        def search(self, q, limit=10, filters=None):
            return [AcademicItem(source="openlibrary", key="/books/OL1M", title="T", item_type="book")]

        def get(self, key):
            return AcademicItem(source="openlibrary", key=key, title="T", item_type="book",
                                url="https://openlibrary.org/books/OL1M")

    def test_metadata_only_backend_has_no_download_hint(self):
        _rc, out = self._run(["fetch", "--book", "x", "--backend", "openlibrary"], self.MetadataOnly())
        self.assertIn("metadata only, no file", out)
        self.assertNotIn("Add --download", out)

    def test_a_public_copy_is_labelled_even_under_a_renamed_backend(self):
        _rc, out = self._run(["fetch", "--book", "x", "--backend", "books"], self.PublicCopy())
        self.assertIn("public copy, not an open-access licence", out)
        self.assertNotIn("record (open access)", out)


class MainCatchTest(unittest.TestCase):
    def test_config_syntax_error_surfaces_as_error(self):
        # A malformed config.toml (TOMLDecodeError) must surface as [error] exit 2, never a traceback.
        import contextlib
        import io
        import tomllib
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
        # A single backend returning a non-JSON body (ValueError) must be skipped, not sink the search.
        import argparse
        import contextlib
        import io
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

        args = argparse.Namespace(all=False, backend=None, query="x", limit=10, format="table",
                                  no_cite=False, type=None, sort=None,
                                  title=None, author=None, year=None, container=None)
        out = io.StringIO()
        err = io.StringIO()
        backend_map = {"bad": _Raises(), "ok": _Ok()}
        with mock.patch("litcli.commands.search.get_all_backends", return_value=backend_map), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cmd_search({}, args)
        self.assertEqual(rc, 0)
        self.assertEqual(err.getvalue(), "")  # silent unless --verbose
        self.assertIn("Good", out.getvalue())


if __name__ == "__main__":
    unittest.main()
