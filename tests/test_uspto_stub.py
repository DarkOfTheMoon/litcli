"""USPTO patent backend offline stub tests: mock urlopen, assert request construction / X-Api-Key / parse / error wrapping."""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from litcli.backends.uspto import UsptoBackend, _pat_to_item

REC = {
    "patent_patent_id": "US10123456B2",
    "patent_title": "Deep battery electrolyte system",
    "patent_date": "2018-03-13",
    "patent_abstract": "A nonflammable electrolyte for lithium-ion batteries.",
    "patent_inventor": [
        {"patent_inventor_first_name": "Wei", "patent_inventor_last_name": "Zhang"},
        {"patent_inventor_first_name": "Li", "patent_inventor_last_name": "Wang"},
    ],
}
DATA = {"results": [REC], "total_results": 1}


class _Resp:
    def __init__(self, data):
        self._d = data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps(self._d).encode()


class UsptoStubTest(unittest.TestCase):
    def test_search_request_shape(self):
        b = UsptoBackend(api_key="KEY123")
        with mock.patch("urllib.request.urlopen", return_value=_Resp(DATA)) as m:
            items = b.search("battery electrolyte")
        req = m.call_args[0][0]
        self.assertEqual(req.method, "POST")
        self.assertEqual(req.full_url, "https://api.uspto.gov/api/v1/patent/search")
        hdrs = {k.lower(): v for k, v in req.headers.items()}
        self.assertEqual(hdrs.get("x-api-key"), "KEY123")
        body = json.loads(req.data.decode())
        self.assertEqual(body["query"]["fq"], ["battery electrolyte"])
        self.assertEqual(len(items), 1)
        it = items[0]
        self.assertEqual(it.source, "uspto")
        self.assertEqual(it.key, "US10123456B2")
        self.assertEqual(it.item_type, "patent")
        self.assertEqual(it.year, "2018")
        self.assertIn("battery", it.title.lower())
        self.assertEqual(len(it.creators), 2)
        self.assertEqual(it.creators[0]["family"], "Zhang")
        self.assertEqual(it.creators[0]["given"], "Wei")
        self.assertIn("US10123456B2", it.url)

    def test_no_key_raises_clear(self):
        b = UsptoBackend(api_key="")
        with self.assertRaises(RuntimeError) as ctx:
            b.search("battery")
        self.assertIn("api_key", str(ctx.exception))

    def test_403_wrapped_with_hint(self):
        from urllib.error import HTTPError
        err = HTTPError("u", 403, "Forbidden", None, None)
        with mock.patch("urllib.request.urlopen", side_effect=err):
            b = UsptoBackend(api_key="BAD")
            with self.assertRaises(RuntimeError) as ctx:
                b.search("battery")
        self.assertIn("403", str(ctx.exception))
        self.assertIn("api_key", str(ctx.exception))

    def test_get_not_found(self):
        no = {"results": [], "total_results": 0}
        with mock.patch("urllib.request.urlopen", return_value=_Resp(no)):
            b = UsptoBackend(api_key="K")
            with self.assertRaises(KeyError):
                b.get("US00000000A1")

    def test_pat_to_item_tolerates_missing(self):
        it = _pat_to_item({"patent_title": "T", "patent_date": "2020-01-01"})
        self.assertEqual(it.key, "")
        self.assertEqual(it.creators, [])
        self.assertEqual(it.year, "2020")
        self.assertEqual(it.item_type, "patent")


if __name__ == "__main__":
    unittest.main()
