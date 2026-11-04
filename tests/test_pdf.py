"""Offline unit tests for pdf.py (download, extract_text, first_lines)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from litcli import pdf as pdfutil
from tests.http_stub import Recorder, client_spy


class _Tmp:
    def __init__(self, *a, **k):
        self.name = "/fake/tmp.pdf"
    def write(self, b):
        pass
    def close(self):
        pass


class DownloadTest(unittest.TestCase):
    def test_download_returns_bytes_with_ua(self):
        rec = Recorder((200, b"pdf-data"))
        data = pdfutil.download("http://x/y.pdf", transport=rec)
        self.assertEqual(data, b"pdf-data")
        self.assertEqual(rec.urls[0], "http://x/y.pdf")
        self.assertTrue(rec.header("user-agent").startswith("litcli/0.1"))

    def test_download_uses_the_configured_proxy(self):
        from unittest import mock

        from litcli.transport import set_proxy

        seen = {}
        set_proxy("http://proxy:8080")
        try:
            with mock.patch.object(pdfutil.httpx, "Client", client_spy(seen, b"pdf-data")):
                pdfutil.download("http://x/y.pdf")
        finally:
            set_proxy("")
        self.assertEqual(seen["proxy"], "http://proxy:8080")

    def test_download_rejects_non_http_scheme(self):
        # A file:// (or ftp://) URL must be refused outright — it could read local files.
        with self.assertRaises(ValueError):
            pdfutil.download("file:///etc/passwd")
        with self.assertRaises(ValueError):
            pdfutil.download("ftp://example.com/x.pdf")


class ExtractTextTest(unittest.TestCase):
    def _proc(self, retcode, stdout):
        p = unittest.mock.Mock()
        p.returncode = retcode
        p.stdout = stdout
        return p

    def test_from_path(self):
        from unittest import mock
        proc = self._proc(0, "Hello\nWorld\n")  # extract_text only strips whole-string ends
        with mock.patch("litcli.pdf.subprocess.run", return_value=proc) as m:
            text = pdfutil.extract_text("/tmp/a.pdf")
        self.assertEqual(text, "Hello\nWorld")
        self.assertEqual(m.call_args[0][0], ["pdftotext", "-layout", "/tmp/a.pdf", "-"])

    def test_first_page_flags(self):
        from unittest import mock
        proc = self._proc(0, "T")
        with mock.patch("litcli.pdf.subprocess.run", return_value=proc) as m:
            pdfutil.extract_text("/tmp/a.pdf", first_page=True)
        self.assertEqual(m.call_args[0][0],
                         ["pdftotext", "-layout", "-f", "1", "-l", "1", "/tmp/a.pdf", "-"])

    def test_nonzero_returncode_returns_empty(self):
        from unittest import mock
        proc = self._proc(1, "junk")
        with mock.patch("litcli.pdf.subprocess.run", return_value=proc):
            self.assertEqual(pdfutil.extract_text("/tmp/a.pdf"), "")

    def test_pdftotext_missing_returns_empty(self):
        from unittest import mock
        with mock.patch("litcli.pdf.subprocess.run", side_effect=FileNotFoundError):
            self.assertEqual(pdfutil.extract_text("/tmp/a.pdf"), "")

    def test_bytes_path_writes_temp_and_cleans_up(self):
        from unittest import mock
        proc = self._proc(0, "From bytes\n")
        with mock.patch("litcli.pdf.tempfile.NamedTemporaryFile", return_value=_Tmp()), \
             mock.patch("litcli.pdf.subprocess.run", return_value=proc) as m, \
             mock.patch("litcli.pdf.os.unlink") as unlink:
            text = pdfutil.extract_text(b"pdf-bytes")
        self.assertEqual(text, "From bytes")
        self.assertEqual(m.call_args[0][0], ["pdftotext", "-layout", "/fake/tmp.pdf", "-"])
        unlink.assert_called_once_with("/fake/tmp.pdf")

    def test_max_chars_truncates(self):
        from unittest import mock
        proc = self._proc(0, "x" * 100)
        with mock.patch("litcli.pdf.subprocess.run", return_value=proc):
            text = pdfutil.extract_text("/tmp/a.pdf", max_chars=50)
        self.assertTrue(text.startswith("x" * 50))
        self.assertTrue(text.endswith("\n[truncated]"))


class FirstLinesTest(unittest.TestCase):
    def test_returns_first_n_nonempty_lines(self):
        text = "line1\n\n  line2  \nline3\n"
        self.assertEqual(pdfutil.first_lines(text, 2), "line1\nline2")
        self.assertEqual(pdfutil.first_lines(text, 5), "line1\nline2\nline3")


if __name__ == "__main__":
    unittest.main()
