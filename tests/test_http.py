"""Unit tests for the shared HTTP transport.

Everything here runs against ``httpx.MockTransport`` (``tests.http_stub``), so the retry
policy, the error wrapping and the pacing are exercised without a socket.
"""
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

import httpx
from tenacity import wait_none

import litcli.transport as transport_mod
from litcli.backends.http import HttpBackend, HttpError, retry_after_seconds
from litcli.errors import Unavailable
from litcli.transport import Session, set_proxy
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


class SessionCloseTest(unittest.TestCase):
    def tearDown(self):
        set_proxy("")

    def test_close_waits_for_a_client_that_is_being_built(self):
        # Without the builder's lock, close() saw ``_client is None``, returned, and the client
        # the other thread was still constructing was then never closed.
        entered, release = threading.Event(), threading.Event()

        class SlowClient(httpx.Client):
            def __init__(self, *args, **kwargs):
                entered.set()
                release.wait(5)
                super().__init__(*args, **kwargs)

        with mock.patch.object(transport_mod.httpx, "Client", SlowClient):
            session = Session(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
            build = threading.Thread(target=lambda: session.client)
            build.start()
            self.assertTrue(entered.wait(5))
            closer = threading.Thread(target=session.close)
            closer.start()
            closer.join(0.1)
            self.assertTrue(closer.is_alive())  # blocked on the lock, not past the builder
            release.set()
            build.join(5)
            closer.join(5)
        self.assertFalse(build.is_alive())
        self.assertIsNone(session._client)


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

    def test_the_retry_budget_is_configurable(self):
        # The retry budget is a per-site matter (a fan-out source and a single-item reader
        # differ): as a class attribute alone, `retries = 5` in a config section was a
        # TypeError and the only way to raise it was to edit the code.
        be = _Backend(retries=5)
        rec = stub(be, (503, "busy"))
        with self.assertRaises(HttpError):
            be.fetch("https://x/api")
        self.assertEqual(len(rec.requests), 5)

    def test_the_retry_after_cap_is_configurable(self):
        # A site whose penalty window runs in minutes asks for more than the 30 s default; the
        # cap is what stops one `Retry-After` from eating the whole run.
        self.assertEqual(retry_after_seconds(self._with_retry_after("600"), 600), 600.0)
        self.assertEqual(retry_after_seconds(self._with_retry_after("600")), 30.0)

    @staticmethod
    def _with_retry_after(value):
        response = httpx.Response(429, headers={"Retry-After": value},
                                  request=httpx.Request("GET", "https://x/api"))
        return httpx.HTTPStatusError("429", request=response.request, response=response)

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
        with self.assertRaises(Unavailable) as cm:
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
        # Three gaps of 20 ms between four callers, whatever the thread interleaving. Windows'
        # ~15.6 ms timer granularity shortens each sleep, so a third of the window is slack;
        # a throttle that doesn't serialise finishes in well under 20 ms.
        self.assertGreaterEqual(time.monotonic() - start, 0.04)

    def test_a_seeded_cookie_survives_a_redirect(self):
        # httpx drops the ``Cookie`` header on every redirect and rebuilds it from the jar, so
        # a hand-supplied cookie must live in the jar (a DOI key starts at ``doi.org``).
        be = _Backend()
        be.seed_cookies, be.seed_hosts = {"cf_clearance": "abc"}, ("x.test",)
        rec: Recorder = stub(be, (302, "", {"location": "https://x.test/a"}), (200, ""))
        be.fetch_text("https://doi.test/10.1/x")
        self.assertEqual(rec.cookies, [None, "cf_clearance=abc"])

    def test_a_seeded_cookie_is_scoped_to_its_hosts(self):
        be = _Backend()
        be.seed_cookies, be.seed_hosts = {"cf_clearance": "abc"}, ("x.test",)
        rec: Recorder = stub(be, (200, ""))
        be.fetch_text("https://elsewhere.test/a")
        self.assertEqual(rec.cookies, [None])

    def test_a_server_refresh_sticks_in_the_jar(self):
        be = _Backend()
        rec: Recorder = stub(be, (200, "", {"set-cookie": "fresh=1; Path=/"}),
                             (200, "", {"set-cookie": "fresh=2; Path=/"}))
        be.fetch_text("https://x/a")
        be.fetch_text("https://x/b")
        self.assertEqual(rec.cookies, [None, "fresh=1"])

    def test_recorder_records_urls(self):
        be = _Backend()
        rec: Recorder = stub(be, (200, {}))
        be.fetch("https://x/api?a=1")
        self.assertEqual(rec.urls, ["https://x/api?a=1"])


if __name__ == "__main__":
    unittest.main()
