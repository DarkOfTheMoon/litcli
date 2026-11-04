"""Unit tests for the shared HTTP transport.

Everything here runs against ``httpx.MockTransport`` (``tests.http_stub``), so the retry
policy, the error wrapping and the pacing are exercised without a socket.
"""
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

import httpx
from tenacity import wait_none

from litcli.transport import set_proxy
from litcli.backends.http import HttpBackend, HttpError
from tests.http_stub import Recorder, error, stub


class _Backend(HttpBackend):
    name = "stub"
    retry_wait = wait_none()

    def _throttle(self):  # the limiter would really sleep between tests
        pass

    def search(self, query, limit=10, filters=None):
        return []

    def get(self, key):
        raise KeyError(key)


class TransportTest(unittest.TestCase):
    def tearDown(self):
        set_proxy("")

    def test_json_is_parsed(self):
        be = _Backend()
        stub(be, (200, {"a": 1}))
        self.assertEqual(be.fetch_json("https://x/api"), {"a": 1})

    def test_text(self):
        be = _Backend()
        stub(be, (200, "hello"))
        self.assertEqual(be.fetch_text("https://x/"), "hello")

    def test_bytes(self):
        be = _Backend()
        stub(be, (200, b"%PDF-"))
        self.assertEqual(be.fetch_bytes("https://x/f"), b"%PDF-")

    def test_5xx_is_retried_then_succeeds(self):
        be = _Backend()
        rec = stub(be, (503, "busy"), (200, {"ok": True}))
        self.assertEqual(be.fetch_json("https://x/api"), {"ok": True})
        self.assertEqual(len(rec.requests), 2)

    def test_5xx_exhausts_and_raises_http_error(self):
        be = _Backend()
        rec = stub(be, (500, "boom"))
        with self.assertRaises(HttpError) as cm:
            be.fetch("https://x/api")
        self.assertEqual(cm.exception.status, 500)
        self.assertEqual(cm.exception.body, "boom")
        self.assertEqual(len(rec.requests), be.retries)

    def test_404_is_not_retried(self):
        be = _Backend()
        rec = stub(be, (404, "missing"))
        with self.assertRaises(HttpError) as cm:
            be.fetch("https://x/api")
        self.assertEqual(cm.exception.status, 404)
        self.assertEqual(len(rec.requests), 1)

    def test_raw_mode_returns_the_4xx_response(self):
        be = _Backend()
        stub(be, (404, "missing"))
        r = be.request("GET", "https://x/api", raise_for_status=False)
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.text, "missing")

    def test_transport_error_retries_then_raises_runtime_error(self):
        be = _Backend()
        be._transport = error(httpx.ConnectError("down"))
        with self.assertRaises(RuntimeError) as cm:
            be.fetch("https://x/api")
        self.assertIn("unreachable", str(cm.exception))

    def test_invalid_json_propagates_without_retry(self):
        be = _Backend()
        rec = stub(be, (200, "not-json"))
        with self.assertRaises(ValueError):
            be.fetch_json("https://x/api")
        self.assertEqual(len(rec.requests), 1)

    def test_client_carries_ua_headers_and_proxy(self):
        set_proxy("http://proxy:8080")
        be = _Backend()
        try:
            self.assertTrue(be.http._mounts)  # a proxy mount was installed
            self.assertIn("litcli/", be.http.headers["user-agent"])
        finally:
            be.close()

    def test_throttle_honours_min_interval(self):
        class _Paced(_Backend):
            def _throttle(self):
                return HttpBackend._throttle(self)

        be = _Paced(min_interval=60)
        be._last = time.monotonic()
        with mock.patch("time.sleep") as sleep:
            be._throttle()
        self.assertTrue(sleep.called)

    def test_throttle_serializes_concurrent_callers(self):
        class _Paced(_Backend):
            def _throttle(self):
                return HttpBackend._throttle(self)

        be = _Paced(min_interval=0.02)
        start = time.monotonic()
        with ThreadPoolExecutor(4) as ex:
            list(ex.map(lambda _: be._throttle(), range(4)))
        # Three gaps of 20 ms between four callers, whatever the thread interleaving.
        self.assertGreaterEqual(time.monotonic() - start, 0.05)

    def test_recorder_records_urls(self):
        be = _Backend()
        rec: Recorder = stub(be, (200, {}))
        be.fetch("https://x/api?a=1")
        self.assertEqual(rec.urls, ["https://x/api?a=1"])


if __name__ == "__main__":
    unittest.main()
