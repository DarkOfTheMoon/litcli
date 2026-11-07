"""HTTP transport primitives shared by the backend layer and the domain layer.

Two things live here because both the backends and plain domain helpers (``pdf``,
``unpaywall``) need them: the configured proxy and the factory that builds a configured
``httpx.Client``. Keeping them out of ``backends/`` stops the domain modules from depending
on the backend package, and keeps the proxy in one place instead of a global in ``base``.

``backends/http.py`` builds the retrying, rate-limited ``HttpBackend`` on top of this.
"""
from __future__ import annotations

import httpx

from .errors import LitcliError, Unavailable, http_kind

UA = "litcli/0.1 (+https://gitee.com/simult/litcli)"

# Proxy from ``[network].proxy``. ``httpx`` otherwise only reads HTTP(S)_PROXY from the
# environment, which a config file must not silently rely on.
_PROXY = ""


def set_proxy(url: str) -> None:
    """Remember the configured proxy for new clients ("" clears it)."""
    global _PROXY
    _PROXY = url or ""


def proxy() -> str:
    """The configured proxy, read by every new client."""
    return _PROXY


def client(timeout: float = 30.0, ua: str = UA,
           transport: httpx.BaseTransport | None = None) -> httpx.Client:
    """A short-lived client with the house defaults (redirects and proxy applied)."""
    return httpx.Client(headers={"User-Agent": ua}, timeout=httpx.Timeout(timeout),
                        follow_redirects=True, proxy=proxy() or None, transport=transport)


class HttpError(Unavailable):
    """A non-retryable HTTP failure, classified by status into a :class:`LitcliError` kind.

    ``status`` lets callers add diagnostics for specific codes and ``body`` keeps the response
    text (bot-check pages need it to be answered). ``kind`` is what fall-back policy reads;
    a backend whose site means something else by a code reclassifies at its own call site.
    """

    def __init__(self, backend: str, status: int, reason: str, body: str = ""):
        self.status = status
        self.reason = reason
        self.body = body
        self.kind = http_kind(status).kind
        LitcliError.__init__(self, f"[{backend}] HTTP {status}: {reason}")


class Session:
    """A cookie-keeping client with the house defaults (pooling, proxy, redirects).

    ``HttpBackend`` builds on it, and so do the bot-check backends (Sci-Hub, Z-Library),
    which read a status code as a page state and need the cookie jar across requests.
    The client itself is lazy so merely constructing a backend stays cheap.
    """

    def __init__(self, timeout: float = 30.0, ua: str = UA,
                 transport: httpx.BaseTransport | None = None, headers: dict | None = None,
                 follow_redirects: bool = True, http2: bool = False):
        self.timeout = timeout
        self.UA = ua
        self.headers = headers or {}
        self.follow_redirects = follow_redirects
        self.http2 = http2
        self._transport = transport
        self._client: httpx.Client | None = None

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                headers={"User-Agent": self.UA, **self.headers},
                timeout=httpx.Timeout(self.timeout), follow_redirects=self.follow_redirects,
                http2=self.http2, proxy=proxy() or None, transport=self._transport)
        return self._client

    def has_cookie(self, suffix: str) -> bool:
        """True when the jar holds a non-empty cookie whose name ends with ``suffix``."""
        return any(c.name.endswith(suffix) and c.value for c in self.client.cookies.jar)

    def set_cookie(self, url: str, name: str, value: str) -> None:
        """Place a cookie in the jar for the host of ``url``."""
        host = httpx.URL(url).host
        self.client.cookies.set(name, value, domain=host, path="/")

    def cookie(self, name: str) -> str:
        """A cookie value from the jar ("" when absent)."""
        return next((c.value for c in self.client.cookies.jar if c.name == name), "")

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
