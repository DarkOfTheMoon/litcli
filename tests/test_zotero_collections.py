'''Zotero collection-listing stub tests: stub http.server, assert collection_tree / collection_items membership logic.

Key background: Zotero's /items?collection=X is actually ignored (returns the whole library), so
collection_items must "pull all items + assign membership locally by each item's collections field".
Here a stub server pins that behavior, and verifies recursive (with sub-collections) vs non-recursive
(only this collection) semantics.
'''
import json
import threading
import unittest
import sys, os
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from litcli.backends.zotero_local import ZoteroLocalBackend

# Collection tree:
#   R1 (root)
#     ├─ C1
#     │   └─ C1a
#     └─ C2
# Items: A in R1, B in C1a, C in C2, D in no collection (orphan)
COLLECTIONS = [
    {"key": "R1",  "data": {"key": "R1",  "name": "Root",  "parentCollection": ""}},
    {"key": "C1",  "data": {"key": "C1",  "name": "Sub1",  "parentCollection": "R1"}},
    {"key": "C1a", "data": {"key": "C1a", "name": "Sub1a", "parentCollection": "C1"}},
    {"key": "C2",  "data": {"key": "C2",  "name": "Sub2",  "parentCollection": "R1"}},
]

def _item(key, title, colls):
    return {"key": key, "data": {
        "key": key, "itemType": "journalArticle", "title": title,
        "creators": [], "date": "2000", "abstractNote": "", "DOI": "", "url": "",
        "collections": colls,
    }}

ITEMS = [
    _item("A", "Alpha", ["R1"]),
    _item("B", "Beta", ["C1a"]),
    _item("C", "Gamma", ["C2"]),
    _item("D", "Orphan", []),
]


class _Handler(BaseHTTPRequestHandler):
    def _send(self, obj, status=200, extra=None):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        # Actual request path looks like /api/users/0/... (base contains /api); match by substring
        if "/collections" in self.path:
            return self._send(COLLECTIONS)
        if "/items" in self.path:
            # Pagination: slice by start, per-page size decided by the client's limit
            import urllib.parse
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            start = int(q.get("start", ["0"])[0])
            size = int(q.get("limit", ["100"])[0])
            page = ITEMS[start:start + size]
            return self._send(page)
        return self._send([])

    def log_message(self, *a):
        pass


class ZoteroCollectionsStubTest(unittest.TestCase):
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
        self.be = ZoteroLocalBackend(
            name="zotero", base="http://127.0.0.1:%d/api" % self.port, library="0"
        )

    def test_collection_tree_returns_parents(self):
        nodes = {n["key"]: n for n in self.be.collection_tree()}
        self.assertEqual(nodes["R1"]["parent"], "")
        self.assertEqual(nodes["C1"]["parent"], "R1")
        self.assertEqual(nodes["C1a"]["parent"], "C1")
        self.assertTrue(nodes["C1"]["name"] and nodes["C1a"]["name"])

    def test_collection_items_recursive_includes_descendants(self):
        # R1 recursive: should include R1 itself (A) + sub-collection C1a (B) + C2 (C); not the orphan D
        items = self.be.collection_items("R1", recursive=True)
        keys = {it.key for it in items}
        self.assertEqual(keys, {"A", "B", "C"})

    def test_collection_items_non_recursive_only_self(self):
        # R1 non-recursive: only A directly in R1
        items = self.be.collection_items("R1", recursive=False)
        keys = {it.key for it in items}
        self.assertEqual(keys, {"A"})

    def test_collection_items_deep_sub_collection(self):
        # C1a alone (recursive, no sub-collections): only B
        items = self.be.collection_items("C1a", recursive=True)
        self.assertEqual({it.key for it in items}, {"B"})

    def test_items_carry_collections_extra(self):
        items = self.be.collection_items("C1a", recursive=True)
        for it in items:
            self.assertIn("collections", it.extra)

    def test_non_recursive_skips_collection_tree(self):
        # `data members <coll> --no-recursive` only needs this collection's direct members, so it
        # must not paginate the collection tree (a real cost on a large library with many collections).
        from unittest import mock
        calls = []

        def fake_paginate(path, params, size=100):
            calls.append(path)
            return COLLECTIONS if "/collections" in path else ITEMS

        with mock.patch.object(self.be, "_paginate", side_effect=fake_paginate):
            items = self.be.collection_items("R1", recursive=False)
        self.assertEqual({it.key for it in items}, {"A"})
        self.assertFalse(any("/collections" in p for p in calls))


class ZoteroSearchParseTest(unittest.TestCase):
    def setUp(self):
        self.be = ZoteroLocalBackend(name="zotero", base="http://localhost:23119/api", library="0")

    def test_search_passes_qmode_everything(self):
        # Chinese/abstract-field search needs qmode=everything (default titleCreatorYear misses abstracts)
        from unittest import mock
        with mock.patch.object(self.be, "_json_path") as m:
            m.return_value = ({}, [])
            self.be.search("非正交 扩散", limit=5)
            _, params = m.call_args[0]
            self.assertEqual(params["qmode"], "everything")
            self.assertEqual(params["q"], "非正交 扩散")
            self.assertEqual(params["limit"], 5)

    def test_search_folds_field_filters_into_q(self):
        # zotero only has a single q: field filter words fold into q (qmode=everything hits all fields)
        from unittest import mock
        with mock.patch.object(self.be, "_json_path") as m:
            m.return_value = ({}, [])
            self.be.search("anisotropic", filters={"title": "mesh", "author": "Droniou", "year": "2018"})
            _, params = m.call_args[0]
            self.assertEqual(params["q"], "anisotropic mesh Droniou 2018")
            self.assertEqual(params["qmode"], "everything")

    def test_item_to_academic_reads_container_fields(self):
        from litcli.backends.zotero_local import _item_to_academic
        it = _item_to_academic({
            "key": "K1", "itemType": "journalArticle", "title": "T",
            "publicationTitle": "J. Fluid Mech.", "volume": "45", "issue": "3",
            "pages": "1-9", "publisher": "ACME", "date": "2002"})
        self.assertEqual(it.container_title, "J. Fluid Mech.")
        self.assertEqual(it.volume, "45")
        self.assertEqual(it.issue, "3")
        self.assertEqual(it.page, "1-9")
        self.assertEqual(it.publisher, "ACME")

    def test_item_to_academic_falls_back_to_book_title(self):
        from litcli.backends.zotero_local import _item_to_academic
        it = _item_to_academic({"key": "K", "itemType": "bookSection", "title": "Ch",
                                "bookTitle": "Big Book", "date": "2001"})
        self.assertEqual(it.container_title, "Big Book")

    def test_ping(self):
        from unittest import mock
        with mock.patch.object(self.be, "_request", return_value=(200, {}, b"[]")):
            self.assertTrue(self.be.ping())
        with mock.patch.object(self.be, "_request", return_value=(503, {}, b"")):
            self.assertFalse(self.be.ping())


if __name__ == "__main__":
    unittest.main()
