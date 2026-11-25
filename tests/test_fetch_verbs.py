"""``fetch`` still understands the 0.1 grammar, which stale skill copies keep teaching.

``fetch fulltext <doi>`` / ``fetch book <title>`` / ``--download`` used to be the only forms;
letting them fail with a bare argparse usage costs the reader the whole command.
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from litcli.commands import fetch
from litcli.commands.parser import build_parser

URL = ("https://pdf.sciencedirectassets.com/271985/1-s2.0-S1270963819310983/am.pdf"
       "?X-Amz-Expires=300&pii=S1270963819310983")


class ParseTest(unittest.TestCase):
    def test_the_old_verb_and_flag_still_parse(self):
        a = build_parser().parse_args(["fetch", "fulltext", "10.1/x", "--download"])
        self.assertEqual((a.target, a.extra, a.download), ("fulltext", ["10.1/x"], True))

    def test_an_unquoted_title_parses(self):
        a = build_parser().parse_args(["fetch", "--book", "finite", "volume", "method"])
        self.assertEqual(a.extra, ["volume", "method"])


class DispatchTest(unittest.TestCase):
    def _run(self, argv):
        args = build_parser().parse_args(argv)
        full, book = mock.Mock(return_value=0), mock.Mock(return_value=0)
        with mock.patch.object(fetch, "_fetch_fulltext", full), \
                mock.patch.object(fetch, "_fetch_book", book):
            fetch.cmd_fetch({}, args)
        return full, book

    def test_fulltext_is_a_verb_not_a_target(self):
        full, book = self._run(["fetch", "fulltext", "10.1/x"])
        self.assertEqual(full.call_args.args[2], "10.1/x")
        book.assert_not_called()

    def test_book_before_an_isbn_keeps_the_book_path(self):
        _full, book = self._run(["fetch", "book", "978-7-118-04834-8"])
        self.assertEqual(book.call_args.args[2], "978-7-118-04834-8")

    def test_extra_words_are_joined_back_into_the_target(self):
        full, _ = self._run(["fetch", "fulltext", "10.1/x", "extra"])
        self.assertEqual(full.call_args.args[2], "10.1/x extra")

    def test_a_bare_verb_reports_an_empty_target(self):
        full, _ = self._run(["fetch", "fulltext"])
        full.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class PastedPdfUrlTest(unittest.TestCase):
    """A PDF link handed over in place of a key is a target, not a hint to re-derive from."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _args(self):
        return mock.Mock(out=self.tmp.name, lines=0, no_download=False)

    def _probe(self, info):
        return fetch._Probe(key="pii:S1270963819310983", info=info, email="", scihub_backend=None,
                            publishers={}, pub_errors={}, unresolved=[], pages={})

    def test_the_link_is_downloaded_as_given(self):
        info = {"has_fulltext": True, "direct": True, "format": "pdf", "url": URL,
                "kind": "url"}
        with mock.patch.object(fetch.pdfutil, "download", return_value=b"%PDF-1.4 x") as dl, \
                mock.patch("sys.stdout"):
            self.assertEqual(fetch._emit_fulltext(self._args(), self._probe(info)), 0)
        self.assertEqual(dl.call_args.args[0], URL)
        self.assertEqual(len(os.listdir(self.tmp.name)), 1)

    def test_a_link_that_answers_no_file_still_falls_through_the_chain(self):
        info = {"has_fulltext": True, "direct": True, "format": "pdf", "url": URL, "kind": "url"}
        tail = {"has_fulltext": True, "kind": "unpaywall", "url": "https://x/landing",
                "format": "html"}
        with mock.patch.object(fetch.pdfutil, "download",
                               side_effect=OSError("timed out")), \
                mock.patch.object(fetch.fulltext_mod, "resolve_fulltext", return_value=tail), \
                mock.patch("sys.stdout") as out:
            self.assertEqual(fetch._emit_fulltext(self._args(), self._probe(info)), 0)
        written = "".join(str(call.args[0]) for call in out.write.call_args_list)
        self.assertIn("https://x/landing", written)

    def test_an_html_landing_still_gives_scihub_its_turn(self):
        # `--scihub` is a request for a copy and a landing page is none: ending on the page
        # without even asking Sci-Hub reads as the flag being ignored.
        info = {"has_fulltext": True, "kind": "unpaywall", "url": "https://x/landing",
                "format": "html"}
        probe = self._probe(info)._replace(scihub_backend=mock.Mock())
        tail = {"has_fulltext": True, "kind": "scihub", "url": "https://s/x.pdf", "format": "pdf"}
        with mock.patch.object(fetch.pdfutil, "download", return_value=b"%PDF-1.4 x") as dl, \
                mock.patch.object(fetch.fulltext_mod, "resolve_fulltext", return_value=tail) as rs, \
                mock.patch("sys.stdout"):
            self.assertEqual(fetch._emit_fulltext(self._args(), probe), 0)
        self.assertTrue(rs.call_args.kwargs["tail_only"])
        self.assertEqual(dl.call_args.args[0], "https://s/x.pdf")
        self.assertEqual(len(os.listdir(self.tmp.name)), 1)

    def test_the_landing_still_wins_when_the_tail_has_no_file(self):
        info = {"has_fulltext": True, "kind": "unpaywall", "url": "https://x/landing",
                "format": "html"}
        probe = self._probe(info)._replace(scihub_backend=mock.Mock())
        tail = {"has_fulltext": False, "errors": ["[scihub] no copy"]}
        with mock.patch.object(fetch.fulltext_mod, "resolve_fulltext", return_value=tail) as rs, \
                mock.patch("sys.stdout") as out:
            self.assertEqual(fetch._emit_fulltext(self._args(), probe), 0)
        self.assertTrue(rs.call_args.kwargs["tail_only"])
        written = "".join(str(c.args[0]) for c in out.write.call_args_list)
        self.assertIn("https://x/landing", written)

    def test_a_walled_oa_link_gets_the_browser_after_the_plain_client(self):
        # A keyless open-access host can still answer Cloudflare 403 to every plain client
        # (MDPI does, browser UA included); the configured session is the only way through, and
        # it is a fallback — the cheap path is still tried first.
        info = {"has_fulltext": True, "kind": "unpaywall", "url": "https://m/x.pdf",
                "format": "pdf"}
        browser = mock.Mock()
        browser.alive.return_value = True
        browser.download.return_value = b"%PDF-1.4 x"
        probe = self._probe(info)._replace(browser=browser)
        with mock.patch.object(fetch.pdfutil, "download", side_effect=OSError("403")), \
                mock.patch("sys.stdout"):
            self.assertEqual(fetch._emit_fulltext(self._args(), probe), 0)
        self.assertEqual(browser.download.call_args.args[0], "https://m/x.pdf")
        self.assertEqual(len(os.listdir(self.tmp.name)), 1)

    def test_a_browser_failure_still_names_the_wall_not_the_browser(self):
        # The browser is the fallback; when it fails too, the reason the host gave is the
        # readable one. A dead session must not replace "not a PDF from <url>".
        info = {"has_fulltext": True, "kind": "unpaywall", "url": "https://m/x.pdf",
                "format": "pdf"}
        tail = {"has_fulltext": True, "kind": "scihub", "url": "https://s/x.pdf", "format": "pdf"}
        browser = mock.Mock()
        browser.alive.return_value = True
        browser.download.side_effect = RuntimeError("browser not installed")
        probe = self._probe(info)._replace(browser=browser)
        with mock.patch.object(fetch.pdfutil, "download", return_value=b"<html>403</html>"), \
                mock.patch.object(fetch.fulltext_mod, "resolve_fulltext", return_value=tail), \
                mock.patch("sys.stdout"), mock.patch("sys.stderr"), \
                self.assertRaises(fetch.LitcliError) as caught:
            fetch._emit_fulltext(self._args(), probe)
        self.assertIn("not a PDF from https://s/x.pdf", str(caught.exception))

    def test_the_browser_is_parked_and_detached_after_the_command(self):
        # Either way it ends: a file left up keeps a pdf-renderer alive and its signed address
        # dies in minutes.
        probe = self._probe({"has_fulltext": False})
        browser = mock.Mock()
        probe = probe._replace(browser=browser)
        with mock.patch.object(fetch, "_probe_fulltext", return_value=probe), \
                mock.patch.object(fetch, "_report_no_fulltext", return_value=1):
            fetch._fetch_fulltext({}, self._args(), "10.1/x")
        browser.reset.assert_called_once()
        browser.close.assert_called_once()

    def test_a_pdf_url_normalizes_into_a_direct_probe(self):
        probe = fetch._probe_fulltext({}, self._args(), URL)
        self.assertTrue(probe.info["direct"])
        self.assertEqual(probe.info["url"], URL)
