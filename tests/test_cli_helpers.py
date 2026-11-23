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

    def test_a_snippet_follows_on_an_indented_line(self):
        row = present(self._item(abstract="relevant text"), no_cite=True, snippet=True)
        self.assertTrue(row.splitlines()[1].startswith("    relevant text"))

    def test_a_long_snippet_is_cut_with_an_ellipsis(self):
        row = present(self._item(abstract="A" * 400), no_cite=True, snippet=True)
        self.assertTrue(row.endswith("…"))
        self.assertLess(len(row.splitlines()[1]), 170)

    def test_no_snippet_without_the_flag(self):
        self.assertEqual(len(present(self._item(abstract="x"), no_cite=True).splitlines()), 1)

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
        return rc, buf.getvalue(), err.getvalue()

    def test_download_on_a_metadata_only_backend_reports_the_page(self):
        rc, out, err = self._run(["fetch", "--book", "x", "--backend", "openlibrary"], self.OpenLibrary())
        self.assertEqual(rc, 1)  # the record resolved, but no bytes exist to save
        self.assertIn("has no downloadable file", err)
        # stdio contract: stdout is only the machine-readable link; the reason goes to stderr.
        self.assertEqual(out, "https://openlibrary.org/books/OL1M\n")

    class MetadataOnly(Backend):
        name = "openlibrary"

        def search(self, q, limit=10, filters=None):
            return [AcademicItem(source="openlibrary", key="/books/OL1M", title="T", item_type="book")]

        def get(self, key):
            return AcademicItem(source="openlibrary", key=key, title="T", item_type="book",
                                url="https://openlibrary.org/books/OL1M")

    def test_metadata_only_backend_has_no_download_hint(self):
        _rc, _out, err = self._run(["fetch", "--book", "x", "--backend", "openlibrary"], self.MetadataOnly())
        self.assertIn("metadata only, no file", err)
        self.assertNotIn("Add --download", err)

    def test_a_public_copy_is_labelled_even_under_a_renamed_backend(self):
        _rc, _out, err = self._run(["fetch", "--book", "x", "--backend", "books"], self.PublicCopy())
        self.assertIn("public copy, not an open-access licence", err)
        self.assertNotIn("record (open access)", err)

    def test_an_empty_target_is_an_error(self):
        # A bare legacy verb leaves nothing behind once the verb is stripped.
        rc, out, err = self._run(["fetch", "fulltext"], self.OpenLibrary())
        self.assertEqual((rc, out), (2, ""))
        self.assertIn("[error] empty target", err)

    def test_a_pick_outside_the_candidates_is_an_error(self):
        rc, out, err = self._run(["fetch", "--book", "x", "--backend", "openlibrary", "--pick", "3"],
                                 self.OpenLibrary())
        self.assertEqual(rc, 2)
        self.assertEqual(out, "")
        self.assertIn("--pick 3 is outside 1..1", err)


class FetchFulltextTest(unittest.TestCase):
    """The paper chain end-to-end in ``cmd_fetch``: save / print / refuse, and the exit codes.

    Resolvers and the byte download are injected; the cwd is a scratch directory so a save
    really lands on disk and can be checked.
    """

    def setUp(self):
        import contextlib
        import io
        import tempfile
        from unittest import mock

        self.out, self.err = io.StringIO(), io.StringIO()
        self.dir = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.dir)
        self.addCleanup(os.chdir, self.cwd)
        for patcher in (mock.patch("litcli.commands.fetch.get_all_backends", return_value={}),
                        mock.patch("litcli.commands.fetch._optional_backend", return_value=None)):
            patcher.start()
            self.addCleanup(patcher.stop)
        out_ctx = contextlib.redirect_stdout(self.out)
        err_ctx = contextlib.redirect_stderr(self.err)
        out_ctx.__enter__()
        err_ctx.__enter__()
        self.addCleanup(out_ctx.__exit__, None, None, None)
        self.addCleanup(err_ctx.__exit__, None, None, None)

    def _run(self, argv, info, download=None, sciencedirect=None, tail=None):
        import contextlib
        from unittest import mock

        import litcli.cli as cli

        with contextlib.ExitStack() as stack:
            if tail is None:
                stack.enter_context(mock.patch(
                    "litcli.commands.fetch.fulltext_mod.resolve_fulltext", return_value=info))
            else:  # the tail-only retry after a publisher download failed
                stack.enter_context(mock.patch(
                    "litcli.commands.fetch.fulltext_mod.resolve_fulltext",
                    side_effect=[info, tail]))
            if download is not None:
                stack.enter_context(mock.patch("litcli.commands.fetch.pdfutil.download",
                                               return_value=download))
            if sciencedirect is not None:
                stack.enter_context(mock.patch(
                    "litcli.commands.fetch.get_all_backends",
                    return_value={"sciencedirect": sciencedirect}))
            rc = cli.main(argv)
        return rc, self.out.getvalue(), self.err.getvalue(), os.listdir(self.dir)

    def test_a_missing_fulltext_exits_1_with_the_scihub_hint(self):
        rc, out, err, files = self._run(["fetch", "10.1/x"], {"has_fulltext": False})
        self.assertEqual((rc, out, files), (1, "", []))
        self.assertIn("add --scihub", err)

    def test_saved_bytes_go_to_disk_and_only_the_path_reaches_stdout(self):
        info = {"has_fulltext": True, "kind": "local", "access": "local", "data": b"%PDF-1.4 x",
                "source": "zotero", "key": "K1"}
        rc, out, err, files = self._run(["fetch", "10.1/x", "--lines", "0"], info)
        self.assertEqual(rc, 0)
        self.assertEqual(out, "10.1_x.pdf\n")  # a bare relative name in the cwd; nothing else
        self.assertEqual(files, ["10.1_x.pdf"])
        self.assertIn("Saved 10 bytes to 10.1_x.pdf", err)

    def test_no_download_prints_the_attachment_url_and_writes_nothing(self):
        info = {"has_fulltext": True, "kind": "local", "access": "local", "data": b"%PDF-1.4 x",
                "source": "zotero", "key": "K1", "url": "https://local/K1"}
        rc, out, _err, files = self._run(["fetch", "10.1/x", "--no-download"], info)
        self.assertEqual((rc, out, files), (0, "https://local/K1\n", []))

    def test_a_landing_page_only_returns_the_link_without_downloading(self):
        info = {"has_fulltext": True, "kind": "unpaywall", "format": "html", "url": "https://legal/page"}
        rc, out, err, files = self._run(["fetch", "10.1/x"], info)
        self.assertEqual((rc, out, files), (0, "https://legal/page\n", []))
        self.assertIn("landing page, not a file", err)

    def test_a_paywall_page_instead_of_a_pdf_is_an_error_not_a_saved_file(self):
        info = {"has_fulltext": True, "kind": "openalex", "format": "pdf", "url": "https://x/wall"}
        rc, out, err, files = self._run(["fetch", "10.1/x"], info, download=b"<html>sign in</html>")
        self.assertEqual((rc, out, files), (2, "", []))
        self.assertIn("[error] not a PDF from https://x/wall", err)

    def test_a_save_leaves_no_part_file_and_overwrites_cleanly(self):
        info = {"has_fulltext": True, "kind": "local", "access": "local", "data": b"%PDF-1.4 new",
                "source": "zotero", "key": "K1"}
        written = os.path.join(self.dir, "10.1_x.pdf")
        with open(written, "wb") as f:
            f.write(b"%PDF-1.4 old")
        rc, _out, _err, files = self._run(["fetch", "10.1/x"], info)
        self.assertEqual((rc, files), (0, ["10.1_x.pdf"]))
        with open(written, "rb") as f:
            self.assertEqual(f.read(), b"%PDF-1.4 new")

    def test_xml_fulltext_is_saved_with_an_xml_suffix(self):
        info = {"has_fulltext": True, "kind": "europepmc", "format": "xml", "url": "https://x/fullTextXML"}
        rc, out, _err, files = self._run(["fetch", "10.1/x"], info, download=b"<article/>")
        self.assertEqual(rc, 0)
        self.assertTrue(out.endswith(".xml\n"))
        self.assertEqual(files, [f"{out.strip().rsplit(os.sep, 1)[-1]}"])

    def _probe_kwarg(self, target):
        from unittest import mock

        import litcli.cli as cli

        sentinel = object()
        with mock.patch("litcli.commands.fetch._optional_backend", return_value=sentinel), \
             mock.patch("litcli.commands.fetch.fulltext_mod.resolve_fulltext",
                        return_value={"has_fulltext": False}) as resolve:
            cli.main(["fetch", target])
        return resolve.call_args.kwargs["publishers"].get("sciencedirect")

    def test_an_elsevier_doi_probes_sciencedirect_even_unconfigured(self):
        self.assertIsNotNone(self._probe_kwarg("10.1016/j.cma.2004.11.019"))

    def test_another_publisher_does_not_pay_a_sciencedirect_probe(self):
        self.assertIsNone(self._probe_kwarg("10.1103/PhysRevLett.126.151601"))

    def _wiley_probe(self, target):
        from unittest import mock

        import litcli.cli as cli

        sentinel = object()
        with mock.patch("litcli.commands.fetch._optional_backend", return_value=sentinel), \
             mock.patch("litcli.commands.fetch.fulltext_mod.resolve_fulltext",
                        return_value={"has_fulltext": False}) as resolve:
            cli.main(["fetch", target])
        return resolve.call_args.kwargs["publishers"].get("wiley")

    def test_a_wiley_doi_probes_wiley_even_unconfigured(self):
        self.assertIsNotNone(self._wiley_probe("10.1002/nme.1628"))

    def test_another_publisher_does_not_pay_a_wiley_probe(self):
        self.assertIsNone(self._wiley_probe("10.1103/PhysRevLett.126.151601"))

    def test_a_wiley_url_normalizes_to_its_doi(self):
        from unittest import mock

        import litcli.cli as cli

        with mock.patch("litcli.commands.fetch.fulltext_mod.resolve_fulltext",
                        return_value={"has_fulltext": False}) as resolve:
            cli.main(["fetch", "https://onlinelibrary.wiley.com/doi/abs/10.1002/nme.1628"])
        self.assertEqual(resolve.call_args.args[0], "10.1002/nme.1628")

    def test_a_jphysiol_doi_probes_wiley_too(self):
        self.assertIsNotNone(self._wiley_probe("10.1113/JP280000"))

    def _springer_probe(self, target):
        from unittest import mock

        import litcli.cli as cli

        sentinel = object()
        with mock.patch("litcli.commands.fetch._optional_backend", return_value=sentinel), \
             mock.patch("litcli.commands.fetch.fulltext_mod.resolve_fulltext",
                        return_value={"has_fulltext": False}) as resolve:
            cli.main(["fetch", target])
        return resolve.call_args.kwargs["publishers"].get("springer")

    def test_a_springer_doi_probes_springer_even_unconfigured(self):
        self.assertIsNotNone(self._springer_probe("10.1007/s11831-021-09587-6"))

    def test_a_nature_doi_probes_springer_too(self):
        self.assertIsNotNone(self._springer_probe("10.1038/s41598-024-80509-9"))

    def test_a_wiley_doi_does_not_pay_a_springer_probe(self):
        self.assertIsNone(self._springer_probe("10.1002/nme.1628"))

    def test_a_springer_url_normalizes_to_its_doi(self):
        from unittest import mock

        import litcli.cli as cli

        with mock.patch("litcli.commands.fetch.fulltext_mod.resolve_fulltext",
                        return_value={"has_fulltext": False}) as resolve:
            cli.main(["fetch", "https://link.springer.com/article/10.1007/s11831-021-09587-6"])
        self.assertEqual(resolve.call_args.args[0], "10.1007/s11831-021-09587-6")

    def test_a_nature_url_normalizes_to_its_doi_instead_of_exiting_2(self):
        # nature.com is named by the Springer matcher, so its URLs must enter the chain too.
        from unittest import mock

        import litcli.cli as cli

        with mock.patch("litcli.commands.fetch.fulltext_mod.resolve_fulltext",
                        return_value={"has_fulltext": False}) as resolve:
            cli.main(["fetch", "https://www.nature.com/articles/s41598-024-80509-9"])
        self.assertEqual(resolve.call_args.args[0], "10.1038/s41598-024-80509-9")

    def _cambridge_probe(self, target):
        from unittest import mock

        import litcli.cli as cli

        sentinel = object()
        with mock.patch("litcli.commands.fetch._optional_backend", return_value=sentinel), \
             mock.patch("litcli.commands.fetch.fulltext_mod.resolve_fulltext",
                        return_value={"has_fulltext": False}) as resolve:
            cli.main(["fetch", target])
        return resolve.call_args.kwargs["publishers"].get("cambridge")

    def test_a_cambridge_doi_probes_cambridge_even_unconfigured(self):
        self.assertIsNotNone(self._cambridge_probe("10.1017/jfm.2016.10"))

    def test_another_publisher_does_not_pay_a_cambridge_probe(self):
        self.assertIsNone(self._cambridge_probe("10.1103/PhysRevLett.126.151601"))

    def test_a_cambridge_url_is_resolved_and_its_doi_reaches_the_chain(self):
        # The URL path carries Cambridge's own product id, so only the page can say the DOI.
        from unittest import mock

        import litcli.cli as cli

        class Pub:
            def get(self, key):
                class Item:
                    doi = "10.1017/jfm.2016.10"
                    title = "T"
                    attachments: tuple = ("https://c/x.pdf",)
                    access = ""
                return Item()

        url = ("https://www.cambridge.org/core/journals/jfm/article/abs/cavity/"
               "273B8416BB80A7AE23315B948FD7ACB9")
        with mock.patch("litcli.commands.fetch._optional_backend", return_value=Pub()), \
             mock.patch("litcli.commands.fetch.fulltext_mod.resolve_fulltext",
                        return_value={"has_fulltext": False}):
            rc = cli.main(["fetch", url, "--no-download"])
        self.assertEqual(rc, 0)
        # The page's own link is already in hand, so the chain is not consulted at all.
        self.assertEqual(self.out.getvalue().strip(), "https://c/x.pdf")
        self.assertIn("subscription copy via your institution", self.err.getvalue())

    def test_a_malformed_cambridge_key_exits_2_instead_of_drifting_down_the_chain(self):
        rc, _out, err, _files = self._run(["fetch", "cambridge:"], {"has_fulltext": False})
        self.assertEqual(rc, 2)
        self.assertIn("[error] [cambridge] empty key", err)
        self.assertIn("cambridge:<DOI>", err)

    def _aip_probe(self, target):
        from unittest import mock

        import litcli.cli as cli

        sentinel = object()
        with mock.patch("litcli.commands.fetch._optional_backend", return_value=sentinel), \
             mock.patch("litcli.commands.fetch.fulltext_mod.resolve_fulltext",
                        return_value={"has_fulltext": False}) as resolve:
            cli.main(["fetch", target])
        return resolve.call_args.kwargs["publishers"].get("aip")

    def test_an_aip_doi_probes_aip_even_unconfigured(self):
        self.assertIsNotNone(self._aip_probe("10.1063/1.870872"))

    def test_a_jasa_doi_probes_aip_too(self):
        self.assertIsNotNone(self._aip_probe("10.1121/1.1902154"))

    def test_another_publisher_does_not_pay_an_aip_probe(self):
        self.assertIsNone(self._aip_probe("10.1103/PhysRevLett.126.151601"))

    def test_a_legacy_aip_url_normalizes_to_its_doi(self):
        from unittest import mock

        import litcli.cli as cli

        with mock.patch("litcli.commands.fetch.fulltext_mod.resolve_fulltext",
                        return_value={"has_fulltext": False}) as resolve:
            cli.main(["fetch", "https://aip.scitation.org/doi/abs/10.1063/1.870872"])
        self.assertEqual(resolve.call_args.args[0], "10.1063/1.870872")

    def _guarded_probe(self, name, target):
        from unittest import mock

        import litcli.cli as cli

        sentinel = object()
        with mock.patch("litcli.commands.fetch._optional_backend", return_value=sentinel), \
             mock.patch("litcli.commands.fetch.fulltext_mod.resolve_fulltext",
                        return_value={"has_fulltext": False}) as resolve:
            cli.main(["fetch", target])
        return resolve.call_args.kwargs["publishers"].get(name)

    def test_each_guarded_doi_probes_its_own_backend(self):
        for name, doi in (("acm", "10.1145/3290605.3300233"),
                          ("aps", "10.1103/PhysRevLett.116.061102"),
                          ("iop", "10.1088/1742-6596/1382/1/012030"),
                          ("tandf", "10.1080/17460441.2026.2762809"),
                          ("aiaa", "10.2514/6.2018-1756"),
                          ("ieee", "10.1109/20.951156")):
            with self.subTest(name):
                self.assertIsNotNone(self._guarded_probe(name, doi))

    def test_a_guarded_doi_does_not_pay_the_other_guards(self):
        probe = self._guarded_probe
        for other in ("acm", "iop", "tandf", "aiaa", "ieee"):
            with self.subTest(other):
                self.assertIsNone(probe(other, "10.1103/PhysRevLett.116.061102"))

    def test_a_guarded_page_url_normalizes_to_its_doi(self):
        from unittest import mock

        import litcli.cli as cli

        with mock.patch("litcli.commands.fetch.fulltext_mod.resolve_fulltext",
                        return_value={"has_fulltext": False}) as resolve:
            cli.main(["fetch", "https://link.aps.org/doi/10.1103/PhysRevLett.116.061102"])
        self.assertEqual(resolve.call_args.args[0], "10.1103/PhysRevLett.116.061102")

    def test_the_site_the_url_lives_on_normalizes_it_first(self):
        # SpringerLink's generic ``/article/<DOI>`` shape would otherwise eat an IOP URL and
        # leave its sub-page on the DOI, which is not the registered form.
        from unittest import mock

        import litcli.cli as cli

        url = "https://iopscience.iop.org/article/10.1088/1674-1137/aea10c/pdf"
        with mock.patch("litcli.commands.fetch.fulltext_mod.resolve_fulltext",
                        return_value={"has_fulltext": False}) as resolve:
            cli.main(["fetch", url])
        self.assertEqual(resolve.call_args.args[0], "10.1088/1674-1137/aea10c")

    def test_an_ieee_document_url_stays_a_url_for_the_page_step(self):
        # The path carries an internal arnumber, so only the page can name the DOI.
        from unittest import mock

        import litcli.cli as cli

        class Pub:
            def get(self, key):
                self.key = key
                return type("I", (), {"doi": "10.1109/20.951156", "title": "T",
                                      "attachments": ("https://e/x.pdf",), "access": ""})()

        pub = Pub()
        url = "https://ieeexplore.ieee.org/document/11682754/"
        with mock.patch("litcli.commands.fetch._optional_backend", return_value=pub), \
             mock.patch("litcli.commands.fetch.fulltext_mod.resolve_fulltext",
                        return_value={"has_fulltext": False}):
            rc = cli.main(["fetch", url, "--no-download"])
        self.assertEqual((rc, self.out.getvalue().strip()), (0, "https://e/x.pdf"))
        self.assertEqual(pub.key, f"url:{url}")

    def test_a_malformed_guarded_key_exits_2(self):
        for name in ("acm", "aps", "iop", "tandf", "aiaa", "ieee"):
            with self.subTest(name):
                rc, _out, err, _files = self._run(["fetch", f"{name}:"], {"has_fulltext": False})
                self.assertEqual(rc, 2)
                self.assertIn(f"[error] [{name}] empty key", err)
                self.assertIn(f"{name}:<DOI>", err)

    def test_a_malformed_aip_key_exits_2_instead_of_drifting_down_the_chain(self):
        rc, _out, err, _files = self._run(["fetch", "aip:"], {"has_fulltext": False})
        self.assertEqual(rc, 2)
        self.assertIn("[error] [aip] empty key", err)
        self.assertIn("aip:<DOI>", err)

    def test_a_failed_aip_download_falls_through_to_the_tail(self):
        from unittest import mock

        import litcli.cli as cli

        class Pub:
            def get_attachment(self, key):
                raise RuntimeError("[aip] no PDF bytes")

        head = {"has_fulltext": True, "kind": "aip", "url": "https://a/article",
                "access": "entitled"}
        tail = {"has_fulltext": True, "kind": "scihub", "url": "https://sci/x.pdf",
                "access": "public"}
        with mock.patch("litcli.commands.fetch.get_all_backends",
                        return_value={"aip": Pub()}), \
             mock.patch("litcli.commands.fetch.fulltext_mod.resolve_fulltext",
                        side_effect=[head, tail]) as resolve, \
             mock.patch("litcli.commands.fetch.pdfutil.download", return_value=b"%PDF-1.4 sci"):
            rc = cli.main(["fetch", "10.1063/x", "--lines", "0"])
        self.assertEqual(rc, 0)
        self.assertTrue(resolve.call_args.kwargs.get("tail_only"))
        self.assertIn("public copy", self.err.getvalue())

    def test_an_oa_link_on_a_publisher_host_downloads_through_that_session(self):
        # Unpaywall often answers with the publisher's own PDF endpoint, which still challenges
        # a bare client: the host, not the hit's kind, decides whose session carries the fetch.
        from unittest import mock

        import litcli.cli as cli

        class Pub:
            def matches_url(self, url):
                return url.startswith("https://iopscience.iop.org/")

            def get_attachment(self, key):
                return b"%PDF-1.4 session"

        head = {"has_fulltext": True, "kind": "unpaywall", "access": "oa",
                "url": "https://iopscience.iop.org/article/10.1088/x/pdf"}
        with mock.patch("litcli.commands.fetch.get_all_backends",
                        return_value={"iop": Pub()}), \
             mock.patch("litcli.commands.fetch.fulltext_mod.resolve_fulltext",
                        return_value=head), \
             mock.patch("litcli.commands.fetch.pdfutil.download") as plain:
            rc = cli.main(["fetch", "10.1088/x", "--lines", "0"])
        self.assertEqual(rc, 0)
        self.assertFalse(plain.called)

    def test_a_malformed_springer_key_exits_2_instead_of_drifting_down_the_chain(self):
        rc, _out, err, _files = self._run(["fetch", "springer:"], {"has_fulltext": False})
        self.assertEqual(rc, 2)
        self.assertIn("[error] [springer] not a DOI", err)

    def test_a_malformed_wiley_key_exits_2_instead_of_drifting_down_the_chain(self):
        rc, _out, err, _files = self._run(["fetch", "wiley:"], {"has_fulltext": False})
        self.assertEqual(rc, 2)
        self.assertIn("[error] [wiley] not a DOI", err)

    def test_a_failed_publisher_download_falls_through_to_the_tail(self):
        # An unentitled Wiley page still answers and names a PDF URL, so the chain stops there;
        # its failed download must not cost the landing page or Sci-Hub their turn.
        from unittest import mock

        import litcli.cli as cli

        class Pub:
            def get_attachment(self, key):
                raise RuntimeError("[wiley] not entitled")

        head = {"has_fulltext": True, "kind": "wiley", "url": "https://w/article",
                "access": "entitled"}
        tail = {"has_fulltext": True, "kind": "scihub", "url": "https://sci/x.pdf",
                "access": "public"}
        with mock.patch("litcli.commands.fetch.get_all_backends",
                        return_value={"wiley": Pub()}), \
             mock.patch("litcli.commands.fetch.fulltext_mod.resolve_fulltext",
                        side_effect=[head, tail]) as resolve, \
             mock.patch("litcli.commands.fetch.pdfutil.download", return_value=b"%PDF-1.4 sci"):
            rc = cli.main(["fetch", "10.1002/x", "--lines", "0"])
        self.assertEqual(rc, 0)
        self.assertTrue(resolve.call_args.kwargs.get("tail_only"))
        self.assertIn("public copy", self.err.getvalue())
        self.assertEqual(os.listdir(self.dir), ["10.1002_x.pdf"])

    def test_a_failed_springer_download_falls_through_to_the_tail(self):
        # A Springer page always names a citation_pdf_url; without entitlement it lands back on
        # the page, and the landing page / Sci-Hub still deserve their turn.
        from unittest import mock

        import litcli.cli as cli

        class Pub:
            def get_attachment(self, key):
                raise RuntimeError("[springer] no PDF bytes")

        head = {"has_fulltext": True, "kind": "springer", "url": "https://s/article",
                "access": "entitled"}
        tail = {"has_fulltext": True, "kind": "scihub", "url": "https://sci/x.pdf",
                "access": "public"}
        with mock.patch("litcli.commands.fetch.get_all_backends",
                        return_value={"springer": Pub()}), \
             mock.patch("litcli.commands.fetch.fulltext_mod.resolve_fulltext",
                        side_effect=[head, tail]) as resolve, \
             mock.patch("litcli.commands.fetch.pdfutil.download", return_value=b"%PDF-1.4 sci"):
            rc = cli.main(["fetch", "10.1007/x", "--lines", "0"])
        self.assertEqual(rc, 0)
        self.assertTrue(resolve.call_args.kwargs.get("tail_only"))
        self.assertIn("public copy", self.err.getvalue())

    def test_an_empty_tail_after_a_failed_publisher_download_gives_the_link(self):
        from unittest import mock

        import litcli.cli as cli

        class Pub:
            def get_attachment(self, key):
                raise RuntimeError("[wiley] not entitled")

        head = {"has_fulltext": True, "kind": "wiley", "url": "https://w/article",
                "access": "entitled"}
        with mock.patch("litcli.commands.fetch.get_all_backends",
                        return_value={"wiley": Pub()}), \
             mock.patch("litcli.commands.fetch.fulltext_mod.resolve_fulltext",
                        side_effect=[head, {"has_fulltext": False}]):
            rc = cli.main(["fetch", "10.1002/x"])
        self.assertEqual(rc, 1)
        self.assertIn("open the PDF in your entitled browser: https://w/article",
                      self.err.getvalue())

    def test_a_blocked_sciencedirect_leaves_its_reason_and_the_cookie_hint(self):
        info = {"has_fulltext": False, "errors": ["<lambda>: ScienceDirect served its gate"]}
        from unittest import mock

        import litcli.cli as cli

        sentinel = object()
        with mock.patch("litcli.commands.fetch._optional_backend", return_value=sentinel), \
             mock.patch("litcli.commands.fetch.fulltext_mod.resolve_fulltext", return_value=info):
            rc = cli.main(["fetch", "10.1016/x"])
        err = self.err.getvalue()
        self.assertEqual(rc, 1)
        self.assertIn("ScienceDirect served its gate", err)
        self.assertIn("cookie_file", err)

    def test_a_sciencedirect_url_is_resolved_and_continues_with_its_doi(self):
        from unittest import mock

        import litcli.cli as cli

        item = type("I", (), {"doi": "10.1016/x", "title": "T", "attachments": []})()
        backend = mock.Mock()
        backend.get.return_value = item
        chain = {"has_fulltext": True, "kind": "openalex", "url": "https://oa/x.pdf"}
        with mock.patch("litcli.commands.fetch.get_all_backends",
                        return_value={"sciencedirect": backend}), \
             mock.patch("litcli.commands.fetch.fulltext_mod.resolve_fulltext",
                        return_value=chain) as resolve:
            rc = cli.main(["fetch", "https://www.sciencedirect.com/science/article/pii/S0045782505000733",
                           "--no-download"])
        self.assertEqual(rc, 0)
        backend.get.assert_called_once_with("pii:S0045782505000733")
        self.assertEqual(resolve.call_args.args[0], "10.1016/x")

    def test_a_blocked_sciencedirect_url_reports_the_gate_without_the_chain(self):
        from unittest import mock

        import litcli.cli as cli

        backend = mock.Mock()
        backend.get.side_effect = RuntimeError("[sciencedirect] gate: supply a cookie")
        with mock.patch("litcli.commands.fetch.get_all_backends",
                        return_value={"sciencedirect": backend}), \
             mock.patch("litcli.commands.fetch.fulltext_mod.resolve_fulltext") as resolve:
            rc = cli.main(["fetch", "S0045782505000733"])
        self.assertEqual(rc, 1)
        resolve.assert_not_called()
        self.assertIn("supply a cookie", self.err.getvalue())

    def test_an_entitled_copy_says_it_is_not_open_access(self):
        from unittest import mock

        # The PDF must come through the backends's own session, not a plain client.
        backend = mock.Mock()
        backend.get_attachment.return_value = b"%PDF-1.4 sd"
        info = {"has_fulltext": True, "kind": "sciencedirect", "access": "entitled",
                "format": "pdf", "url": "https://sd/x.pdf"}
        rc, _out, err, files = self._run(["fetch", "10.1016/x"], info, sciencedirect=backend)
        self.assertEqual(rc, 0)
        self.assertEqual(files, ["10.1016_x.pdf"])
        self.assertIn("subscription copy via your institution", err)
        backend.get_attachment.assert_called_once_with("10.1016/x")

    def test_a_gated_sciencedirect_pdf_reports_the_gate_and_the_link(self):
        from unittest import mock

        from litcli.errors import Unavailable

        backend = mock.Mock()
        backend.get_attachment.side_effect = Unavailable(
            "[sciencedirect] the PDF sits behind a second ScienceDirect acceptance gate")
        info = {"has_fulltext": True, "kind": "sciencedirect", "access": "entitled",
                "format": "pdf", "url": "https://sd/x.pdf"}
        rc, out, err, files = self._run(["fetch", "10.1016/x"], info, sciencedirect=backend,
                                        tail={"has_fulltext": False})
        self.assertEqual((rc, out, files), (1, "", []))
        self.assertIn("acceptance gate", err)
        self.assertIn("https://sd/x.pdf", err)


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
            buf, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
                rc = cli.main(["search", "x"])
        self.assertEqual(rc, 2)
        # Diagnostics go to stderr so a JSON/CSL stdout stays parseable.
        self.assertEqual(buf.getvalue(), "")
        self.assertIn("[error]", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())

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
