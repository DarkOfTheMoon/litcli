"""Offline stand-ins for the browser session layer.

Nothing here starts a browser: :class:`FakeLauncher` answers a launcher call with a runtime
whose pages pop queued ``(status, body)`` pairs (or bytes, for a download), and
:class:`FakeSession` skips the launcher entirely for callers that only need the four methods
``Session`` exposes.
"""
from __future__ import annotations


class FakeResponse:
    def __init__(self, status: int, body: str, url: str):
        self.status = status
        self.headers = {}
        self._body = body
        self.url = url

    @property
    def status_code(self) -> int:
        return self.status

    @property
    def text(self) -> str:
        return self._body

    @property
    def content(self) -> bytes:
        return self._body.encode("utf-8", "replace")

    @property
    def history(self) -> tuple:
        return ()


class FakeDownload:
    """Playwright's download object writes a real file, so this one does too."""

    def __init__(self, data: bytes):
        import tempfile

        handle, self.path = tempfile.mkstemp()
        with open(handle, "wb") as fh:
            fh.write(bytes(data))


class _DownloadCtx:
    def __init__(self, page):
        self._page = page
        self.value = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.value = self._page.last_download


class FakeRequest:
    """The session's request context: same cookies, the bytes and not a viewer."""

    def __init__(self, data=b"%PDF inline"):
        self.data = data
        self.calls = []

    def get(self, url, **kw):
        self.calls.append((url, kw))
        answer = mock_answer(self.data, url)
        return answer


def mock_answer(data, url):
    class _Answer:
        def body(self):
            return data
    return _Answer()


class FakePage:
    def __init__(self, queue):
        self._queue = queue
        self.last_download = None
        self._body = ""
        self._solved = None
        self._listeners = {}
        self.url = ""

    def on(self, event, handler):
        self._listeners.setdefault(event, []).append(handler)

    def remove_listener(self, event, handler):
        self._listeners[event].remove(handler)

    def wait_for_timeout(self, _ms):
        pass

    def goto(self, url, **_kw):
        step = (self._queue.pop(0) if len(self._queue) > 1
                else self._queue[0] if self._queue else (200, ""))
        if isinstance(step, Exception):
            raise step
        if isinstance(step, bytes):
            self.last_download = FakeDownload(step)
            self._body = ""
            for handler in self._listeners.get("download", []):
                handler(self.last_download)
            return FakeResponse(200, "", url)
        status, body, *solved = step
        self._body = body
        self._solved = solved[0] if solved else None
        # A fourth element is where the navigation landed (a redirect into the file itself).
        self.url = solved[-1] if len(solved) > 1 else url
        return FakeResponse(status, body, url)

    def content(self) -> str:
        # A step written as ``(status, body, solved)`` is a challenge that solves itself: the
        # second read is what a browser would get once CF's script has run.
        if self._solved is not None:
            solved, self._solved = self._solved, None
            self._body = solved
        return self._body

    def expect_download(self, **_kw):
        return _DownloadCtx(self)


class FakeRuntime:
    def __init__(self, queue):
        self._page = FakePage(queue)
        self.pages = [self._page]
        self.request = FakeRequest()
        self.closed = False
        self.terminated = False

    def new_page(self):
        return self._page

    def set_default_timeout(self, timeout):
        previous, self.timeout = getattr(self, "timeout", 30000.0), timeout
        return previous

    def close(self):
        self.closed = True

    def terminate(self):
        self.terminated = True
        self.close()

    def stop(self):
        pass


class FakeLauncher:
    """Callable launcher: records the call and serves ``queue``."""

    def __init__(self):
        self.calls = 0
        self.kwargs: dict = {}
        self.queue: list = []
        self.runtime = None

    def __call__(self, profile, headless, channel, proxy):
        self.calls += 1
        self.kwargs = {"profile": profile, "headless": headless, "channel": channel,
                       "proxy": proxy}
        self.runtime = FakeRuntime(self.queue)
        return _Launched(self.runtime)


class _Launched:
    """The shape ``Session`` expects from a launcher: a context plus a stop().

    ``pid`` is this process, the way a bundled browser is a child of the caller.
    """

    def __init__(self, runtime):
        import os

        self.context = runtime
        self.pid = os.getpid()

    def stop(self):
        self.context.close()


class FakeSession:
    """The four methods a caller uses, with no lifetime at all."""

    def __init__(self):
        self.urls: list[str] = []
        self.queue: list = []
        self.alive = lambda: True   # tests reassign it: a dead session takes the cookie path
        self.closed = False
        self.terminated = False

    def start(self):
        pass

    def page(self, url):
        self.urls.append(url)
        return FakePage(self.queue).goto(url)

    def download(self, url):
        self.urls.append(url)
        page = FakePage(self.queue)
        page.goto(url)
        with open(page.last_download.path, "rb") as fh:
            return fh.read()

    def close(self):
        self.closed = True

    def terminate(self):
        self.terminated = True
        self.close()
