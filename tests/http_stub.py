"""Offline transport stubs for the network backends.

``HttpBackend`` takes an ``httpx.BaseTransport``, so a test replaces the network by handing the
backend a :class:`Recorder`: requests are recorded (assert on :attr:`Recorder.urls`) and
answered from a queue of canned responses. Nothing opens a socket.
"""
import httpx
from tenacity import wait_none


class Recorder(httpx.BaseTransport):
    """Transport that records every request and answers with queued ``(status, body)`` pairs.

    ``body`` may be a dict/list (sent as JSON), bytes, or text; a third tuple element adds
    response headers. A single queued response is reused for all requests; several are
    consumed in order (the last one repeats).
    """

    def __init__(self, *responses: tuple):
        if not responses:
            responses = ((200, {}),)
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        step = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        status, body = step[0], step[1]
        headers = step[2] if len(step) > 2 else None
        if isinstance(body, (dict, list)):
            return httpx.Response(status, json=body, headers=headers, request=request)
        if isinstance(body, bytes):
            return httpx.Response(status, content=body, headers=headers, request=request)
        return httpx.Response(status, text=body or "", headers=headers, request=request)

    @property
    def urls(self) -> list[str]:
        return [str(r.url) for r in self.requests]

    def header(self, name: str) -> str:
        """A header value of the most recent request."""
        return self.requests[-1].headers.get(name, "")


def stub(backend, *responses: tuple) -> Recorder:
    """Attach a :class:`Recorder` to ``backend`` and return it (assertions live on it).

    An already-built client is dropped so a second ``stub()`` in one test takes effect.
    """
    backend.close()
    backend._transport = Recorder(*responses)
    return backend._transport


def speed_up(backend) -> None:
    """Turn off retry backoff and per-site pacing for a stubbed backend."""
    backend.retry_wait = wait_none()
    backend._throttle = lambda: None


def error(exc: Exception) -> httpx.MockTransport:
    """A transport that always raises ``exc`` (transport-failure tests)."""
    def boom(request: httpx.Request) -> httpx.Response:
        raise exc

    return httpx.MockTransport(boom)


def client_spy(seen: dict, body: object = b"", status: int = 200) -> type:
    """A stand-in for ``httpx.Client`` that records constructor kwargs and answers one GET.

    Used where the assertion is about how the client is built (the proxy, say) rather than
    about a response: pass a dict ``seen`` and inspect it afterwards.
    """
    class _Client:
        def __init__(self, **kw):
            seen.update(kw)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, *a, **kw):
            if isinstance(body, (dict, list)):
                return httpx.Response(status, json=body)
            return httpx.Response(status, content=body)

    return _Client
