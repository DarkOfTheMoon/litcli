"""Offline unit tests for backends.base helpers: retry_json (shared HTTP retry) and _filters.

retry_json is extracted from crossref/openalex/openlibrary; it had no dedicated coverage, so these
pin its retry semantics (5xx/network backoff + non-5xx no retry) and its JSON parse behavior.
"""
import io
import unittest
from unittest import mock

from litcli.backends import base
from litcli.backends.base import retry_json, Backend


class _Resp(io.BytesIO):
    """A urlopen response: a BytesIO that also behaves as a context manager."""
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


def _http_error(code: int):
    return urllib_error_http(code)


def urllib_error_http(code: int):
    import urllib.error
    return urllib.error.HTTPError("http://x", code, "reason", {}, None)


def urlerror(reason="down"):
    import urllib.error
    return urllib.error.URLError(reason)


class RetryJsonTest(unittest.TestCase):
    """The shared retry helper: success on first try, 5xx retries with backoff, non-5xx no retry."""

    def test_success_first_attempt(self):
        with mock.patch("urllib.request.urlopen", return_value=_Resp(b'{"a":1}')) as m:
            out = retry_json("http://x", {}, "test", timeout=5)
        self.assertEqual(out, {"a": 1})
        self.assertEqual(m.call_count, 1)

    def test_retries_5xx_then_succeeds(self):
        with mock.patch("urllib.request.urlopen",
                        side_effect=[urllib_error_http(503), _Resp(b'{"ok":true}')]), \
             mock.patch("litcli.backends.base.time.sleep") as sleep:
            out = retry_json("http://x", {}, "test", timeout=5)
        self.assertEqual(out, {"ok": True})
        self.assertEqual(sleep.call_args[0][0], 0.5)  # attempt 0 -> 0.5 * (0+1)

    def test_5xx_exhausted_raises(self):
        with mock.patch("urllib.request.urlopen",
                        side_effect=[urllib_error_http(500), urllib_error_http(500),
                                     urllib_error_http(500)]) as m, \
             mock.patch("litcli.backends.base.time.sleep"):
            with self.assertRaises(RuntimeError) as cm:
                retry_json("http://x", {}, "test", timeout=5)
        self.assertIn("HTTP 500", str(cm.exception))
        self.assertEqual(m.call_count, 3)  # all attempts used

    def test_non_5xx_does_not_retry(self):
        # 404 is a hard miss: no backoff, single request.
        with mock.patch("urllib.request.urlopen", side_effect=[urllib_error_http(404)]) as m, \
             mock.patch("litcli.backends.base.time.sleep") as sleep:
            with self.assertRaises(RuntimeError) as cm:
                retry_json("http://x", {}, "test", timeout=5)
        self.assertIn("HTTP 404", str(cm.exception))
        self.assertEqual(m.call_count, 1)
        sleep.assert_not_called()

    def test_urlerror_retries_then_raises(self):
        with mock.patch("urllib.request.urlopen",
                        side_effect=[urlerror("down"), urlerror("down"), urlerror("down")]) as m, \
             mock.patch("litcli.backends.base.time.sleep"):
            with self.assertRaises(RuntimeError) as cm:
                retry_json("http://x", {}, "test", timeout=5)
        self.assertIn("unreachable", str(cm.exception))
        self.assertEqual(m.call_count, 3)

    def test_invalid_json_propagates_without_retry(self):
        # A non-JSON body is a ValueError (json.JSONDecodeError), not a 5xx/network error, so it
        # must propagate immediately rather than be retried.
        with mock.patch("urllib.request.urlopen", return_value=_Resp(b"not-json")) as m, \
             mock.patch("litcli.backends.base.time.sleep") as sleep:
            with self.assertRaises(ValueError):
                retry_json("http://x", {}, "test", timeout=5)
        self.assertEqual(m.call_count, 1)
        sleep.assert_not_called()


class _StubBackend(Backend):
    name = "stub"

    def search(self, query, limit=10, filters=None):
        return []

    def get(self, key):
        raise KeyError(key)


class BaseBackendTest(unittest.TestCase):
    def test_filters_coerces_int_year_to_str(self):
        # _filters must stringify an int year (or .isdigit() would crash on a raw int).
        out = Backend._filters({"year": 2018, "title": "T", "author": None, "container": ""})
        self.assertEqual(out, {"year": "2018", "title": "T"})
        self.assertIsInstance(out["year"], str)

    def test_filters_never_mutates_input(self):
        src = {"title": "T"}
        Backend._filters(src)
        self.assertEqual(src, {"title": "T"})

    def test_can_and_require_write(self):
        self.assertTrue(_StubBackend().can("read"))
        self.assertFalse(_StubBackend().can("write"))

    def test_require_write_raises_not_supported(self):
        with self.assertRaises(base.NotSupportedError):
            _StubBackend()._require("write")


if __name__ == "__main__":
    unittest.main()
