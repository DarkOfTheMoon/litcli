"""Unpaywall resolver offline stub tests: assert URL / parse / error wrapping."""
import os
import sys
import unittest
from unittest import mock

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from litcli import unpaywall
from litcli.errors import BadInput, Unavailable
from litcli.transport import set_proxy
from tests.http_stub import Recorder, client_spy, error

DATA = {"is_oa": True, "oa_status": "gold", "title": "Deep learning", "year": 2015,
        "best_oa_location": {"url_for_pdf": "http://oa.example/p.pdf",
                             "url_for_landing_page": "http://oa.example/landing"}}


class UnpaywallStubTest(unittest.TestCase):
    def test_resolve_url_and_parse(self):
        rec = Recorder((200, DATA))
        info = unpaywall.resolve_oa("10.1038/nature14539", "t@e.com", transport=rec)
        self.assertIn("10.1038%2Fnature14539", rec.urls[0])
        self.assertIn("email=t%40e.com", rec.urls[0])
        self.assertTrue(info["is_oa"])
        self.assertEqual(info["oa_status"], "gold")
        self.assertEqual(info["best_pdf"], "http://oa.example/p.pdf")
        self.assertEqual(info["title"], "Deep learning")

    def test_no_oa(self):
        no = dict(DATA)
        no["is_oa"] = False
        no["best_oa_location"] = None
        info = unpaywall.resolve_oa("10.1/x", "t@e.com", transport=Recorder((200, no)))
        self.assertFalse(info["is_oa"])
        self.assertEqual(info["best_pdf"], "")

    def test_422_wrapped(self):
        with self.assertRaises(BadInput) as ctx:
            unpaywall.resolve_oa("10.1/x", "bad", transport=Recorder((422, "Unprocessable")))
        self.assertIn("422", str(ctx.exception))

    def test_client_uses_the_configured_proxy(self):
        seen = {}
        set_proxy("http://proxy:8080")
        try:
            with mock.patch.object(unpaywall.httpx, "Client", client_spy(seen, DATA)):
                unpaywall.resolve_oa("10.1/x", "t@e.com")
        finally:
            set_proxy("")
        self.assertEqual(seen["proxy"], "http://proxy:8080")

    def test_transport_error_wrapped(self):
        with self.assertRaises(Unavailable):
            unpaywall.resolve_oa("10.1/x", "t@e.com", transport=error(httpx.ConnectError("boom")))


if __name__ == "__main__":
    unittest.main()
