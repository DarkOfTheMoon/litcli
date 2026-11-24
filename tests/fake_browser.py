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


class FakePage:
    def __init__(self, queue):
        self._queue = queue
        self.last_download = None
        self._body = ""

    def goto(self, url, **_kw):
        step = self._queue.pop(0)
        if isinstance(step, bytes):
            self.last_download = FakeDownload(step)
            self._body = ""
            return FakeResponse(200, "", url)
        status, body = step
        self._body = body
        return FakeResponse(status, body, url)

    def content(self) -> str:
        return self._body

    def expect_download(self, **_kw):
        return _DownloadCtx(self)


class FakeRuntime:
    def __init__(self, queue):
        self._page = FakePage(queue)
        self.pages = [self._page]
        self.closed = False

    def new_page(self):
        return self._page

    def close(self):
        self.closed = True

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
    """The shape ``Session`` expects from a launcher: a context plus a stop()."""

    def __init__(self, runtime):
        self.context = runtime

    def stop(self):
        self.context.close()


class FakeSession:
    """The four methods a caller uses, with no lifetime at all."""

    def __init__(self):
        self.urls: list[str] = []
        self.queue: list = []
        self.alive = lambda: True   # tests reassign it: a dead session takes the cookie path
        self.closed = False

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
