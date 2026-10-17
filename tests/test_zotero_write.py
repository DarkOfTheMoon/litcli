'''Zotero local backend write stub tests: stub http.server, assert add/patch request shape.

The purpose is to prove the request shape is correct (METHOD/PATH/HEADERS/BODY), pinning the root
cause of writes not landing to Zotero-side authorization rather than this code.
'''
import json
import threading
import unittest
import sys, os
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from litcli.models import AcademicItem
from litcli.backends.zotero_local import ZoteroLocalBackend


class _Handler(BaseHTTPRequestHandler):
    reqlog = []   # (method, path, headers, body)

    def _send(self, obj=None, status=200, extra=None):
        body = json.dumps(obj).encode() if obj is not None else b""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _record(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(n) if n else b""
        self.reqlog.append((self.command, self.path, dict(self.headers), body))

    def do_GET(self):
        self._record()
        if self.path.startswith("/connector/ping"):
            return self._send({"serverID": "TEST-SID", "version": "3.0.0"})
        import urllib.parse
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if "q" in q:   # search request (q param)
            data = [{"data": {"itemType": "journalArticle", "title": "Wrinkled Drop",
                              "creators": [], "date": "2002", "key": "K1",
                              "abstractNote": "", "DOI": "", "url": ""}}]
            return self._send(data)
        if q.get("limit") == ["1"]:   # library_version probe
            return self._send([], extra={"Last-Modified-Version": "42", "Zotero-Server-ID": "TEST-SID"})
        if "/items/" in self.path:    # get single
            return self._send({"data": {"itemType": "journalArticle", "title": "Old",
                                        "creators": [], "date": "2000", "key": "NEWKEY",
                                        "abstractNote": "", "DOI": "", "url": ""},
                               "version": "43"})
        return self._send([])

    def do_POST(self):
        self._record()
        if "local/authorize" in self.path:
            return self._send({"key": "AUTHKEY123", "remember": True})
        return self._send({"successful": {"0": {"key": "NEWKEY", "version": 43}}})

    def do_PATCH(self):
        self._record()
        return self._send({})

    def do_DELETE(self):
        self._record()
        return self._send({})

    def log_message(self, *a):
        pass


class ZoteroWriteStubTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        _Handler.reqlog = []
        self.be = ZoteroLocalBackend(name="zotero", base="http://127.0.0.1:%d/api" % self.port, library="0")

    def _headers(self, rec):
        return {k.lower(): v for k, v in rec[2].items()}

    def _find(self, method, suffix=None):
        for rec in _Handler.reqlog:
            if rec[0] == method and (suffix is None or suffix in rec[1]):
                return rec
        return None

    def test_add_sends_correct_request(self):
        item = AcademicItem(source="zotero", title="Wrinkled Drop",
                            creators=[{"family": "Doe", "given": "Jane"}],
                            year="2002", doi="10.1/x", item_type="article")
        key = self.be.add(item)
        rec = self._find("POST", "/users/0/items")
        self.assertIsNotNone(rec)
        self.assertEqual(key, "NEWKEY")
        h = self._headers(rec)
        self.assertEqual(h.get("zotero-api-version"), "3")
        self.assertEqual(h.get("zotero-server-id"), "TEST-SID")
        self.assertEqual(h.get("if-unmodified-since-version"), "42")
        # write header carries the local authorization key (obtained via /api/local/authorize)
        self.assertEqual(h.get("zotero-api-key"), "AUTHKEY123")
        body = json.loads(rec[3])
        it = body[0]
        self.assertEqual(it["itemType"], "journalArticle")
        self.assertEqual(it["title"], "Wrinkled Drop")
        self.assertEqual(it["date"], "2002")
        self.assertEqual(it["DOI"], "10.1/x")
        self.assertEqual(it["creators"][0]["firstName"], "Jane")

    def test_patch_sends_correct_request(self):
        self.be.patch("NEWKEY", {"title": "New Title", "year": "2020"})
        rec = self._find("PATCH", "/users/0/items/NEWKEY")
        self.assertIsNotNone(rec)
        body = json.loads(rec[3])
        self.assertEqual(body["title"], "New Title")
        self.assertEqual(body["date"], "2020")
        self.assertEqual(self._headers(rec).get("if-unmodified-since-version"), "43")

    def test_search_reads_items(self):
        items = self.be.search("drop", limit=5)
        self.assertEqual(items[0].title, "Wrinkled Drop")
        self.assertEqual(items[0].year, "2002")

    def test_attach_sends_parented_attachment(self):
        # local path -> linked_file; URL -> linked_url
        att_key = self.be.attach("PARENTKEY", "/home/u/x.pdf")
        self.assertEqual(att_key, "NEWKEY")
        rec = self._find("POST", "/users/0/items")
        self.assertIsNotNone(rec)
        body = json.loads(rec[3])
        att = body[0]
        self.assertEqual(att["itemType"], "attachment")
        self.assertEqual(att["parentItem"], "PARENTKEY")
        self.assertEqual(att["linkMode"], "linked_file")
        self.assertEqual(att["contentType"], "application/pdf")
        self.assertEqual(att["path"], "/home/u/x.pdf")
        self.assertEqual(self._headers(rec).get("if-unmodified-since-version"), "42")

    def test_attach_url_uses_linked_url(self):
        att_key = self.be.attach("PARENTKEY", "https://oa.example/x.pdf")
        self.assertEqual(att_key, "NEWKEY")
        rec = self._find("POST", "/users/0/items")
        body = json.loads(rec[3])
        att = body[0]
        self.assertEqual(att["linkMode"], "linked_url")
        self.assertEqual(att["url"], "https://oa.example/x.pdf")

    def test_delete_sends_delete_request(self):
        self.be.delete("DKEY")
        rec = self._find("DELETE", "/users/0/items/DKEY")
        self.assertIsNotNone(rec)
        self.assertEqual(self._headers(rec).get("if-unmodified-since-version"), "42")

    def test_authorize_obtains_local_key(self):
        key = self.be.authorize()
        self.assertEqual(key, "AUTHKEY123")
        # subsequent write operations use that key directly, no repeated auth dialog (the second authorize still runs once)
        self.assertEqual(self.be._app_key_val, "AUTHKEY123")
        rec = self._find("POST", "local/authorize")
        self.assertIsNotNone(rec)
        h = self._headers(rec)
        self.assertEqual(h.get("zotero-api-version"), "3")
        self.assertEqual(h.get("zotero-server-id"), "TEST-SID")

    def test_write_key_falls_back_to_authorize(self):
        # Without a config key, write operations trigger _write_key -> authorize to get AUTHKEY123
        key = self.be._write_key()
        self.assertEqual(key, "AUTHKEY123")
        # second call hits the cache, no repeat request
        self.be._write_key()
        auths = [r for r in _Handler.reqlog if r[0] == "POST" and "local/authorize" in r[1]]
        self.assertEqual(len(auths), 1)


if __name__ == "__main__":
    unittest.main()
