"""The backend base class for HTTP sources.

On top of :mod:`litcli.transport` (client, proxy, ``HttpError``) this adds what a network
backend needs and a one-off request does not: a pooled lazily built client, retries through
``tenacity`` and a per-site rate through ``pyrate-limiter``. Backends keep only their URLs,
their parsers and their key normalization.

Failures surface as typed :class:`~litcli.errors.LitcliError` subclasses with a ``[backend]`` prefix;
into a ``[skipped]`` backend instead of a stack trace.
"""
from __future__ import annotations

import threading
import time
from typing import Any, ClassVar

import httpx
from pyrate_limiter import Duration, Limiter, Rate
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from .. import debug
from ..debug import log
from ..errors import Unavailable
from ..transport import UA as DEFAULT_UA
from ..transport import HttpError, Session
from .base import Backend

# Transient by nature: rate limiting (429) and the 5xx family, including Cloudflare's own
# 52x codes, which are what these sources answer under load.
RETRY_CODES = frozenset({429, 500, 502, 503, 504, 507, 508, 510, 520, 521, 522, 524})


def _log_retry(state) -> None:
    """Trace a retry decision (tenacity calls this before sleeping)."""
    error = debug.prefix(str(state.outcome.exception())) if state.outcome else ""
    log(f"retry {state.attempt_number} in {state.next_action.sleep:.1f}s: {error}")


def _retryable(exc: BaseException, codes: frozenset[int]) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in codes
    return isinstance(exc, httpx.TransportError)  # includes timeouts and connect errors


class HttpBackend(Session, Backend):
    """Base for backends that talk HTTP.

    Subclasses set ``name``, ``rate``/``min_interval`` (politeness), optionally ``UA``,
    ``http2`` or ``follow_redirects``, and implement ``search``/``get`` against
    :meth:`fetch_json` / :meth:`fetch_text` / :meth:`fetch_bytes`.
    """

    name = ""
    UA = DEFAULT_UA
    #: Extra client-wide request headers (Accept, API key, polite-pool hints).
    headers: ClassVar[dict] = {}
    timeout = 30.0
    #: Minimum seconds between two requests from this backend (0 = rate limit only).
    min_interval = 0.0
    #: Requests per second, enforced by the backend's own limiter.
    rate = 10
    retries = 3
    #: Status codes worth another attempt; backends that read a status as a page state drop it.
    retry_codes = RETRY_CODES
    #: Backoff between retries; tests swap in ``wait_none()``.
    retry_wait = wait_exponential(multiplier=0.5, max=8)
    follow_redirects = True
    http2 = False

    def __init__(self, name: str | None = None, timeout: float | None = None,
                 rate: int | None = None, min_interval: float | None = None):
        if name is not None:
            self.name = name
        if timeout is not None:
            self.timeout = timeout
        if rate is not None:
            self.rate = rate
        if min_interval is not None:
            self.min_interval = min_interval
        super().__init__(timeout=self.timeout, ua=self.UA, headers=self.headers,
                         follow_redirects=self.follow_redirects, http2=self.http2)
        self._send = None
        self._limiter: Limiter | None = None  # built on first use; many backends never send
        self._bucket = f"{self.name or type(self).__name__}-{id(self)}"
        self._last = 0.0
        self._lock = threading.Lock()  # openalex's fan-out paces from several threads

    # -- transport ---------------------------------------------------------------

    @property
    def http(self) -> httpx.Client:
        """The shared client (alias kept because the method bodies read as ``self.http``)."""
        return self.client

    # -- pacing / retry ----------------------------------------------------------

    def _throttle(self) -> None:
        """Space requests: hold the slot for ``min_interval`` and take a limiter permit."""
        with self._lock:
            if self._limiter is None:
                self._limiter = Limiter(Rate(max(1, int(self.rate)), Duration.SECOND))
            if self.min_interval > 0:
                wait = self.min_interval - (time.monotonic() - self._last)
                if wait > 0:
                    log(f"pacing {self.name}: sleeping {wait:.2f}s")
                    time.sleep(wait)
                self._last = time.monotonic()
            self._limiter.try_acquire(self._bucket, blocking=True)

    @property
    def _sender(self):
        """One retrying wrapper per backend (built once, so the policy is not rebuilt per call)."""
        if self._send is None:
            @retry(
                retry=retry_if_exception(lambda exc: _retryable(exc, self.retry_codes)),
                stop=stop_after_attempt(self.retries),
                wait=self.retry_wait,
                reraise=True,
                before_sleep=_log_retry,
            )
            def send(method: str, url: str, *,
                     raise_for_status: bool = True, **kw) -> httpx.Response:
                self._throttle()
                response = self.http.request(method, url, **kw)
                if raise_for_status:
                    response.raise_for_status()
                return response

            self._send = send
        return self._send

    def request(self, method: str, url: str, *,
                raise_for_status: bool = True, **kw) -> httpx.Response:
        """Send a request with pacing and retries.

        ``raise_for_status=False`` hands a 4xx/5xx back as a response instead of raising -
        needed where the status is data (Zotero's API). Transport failures always raise.
        """
        try:
            return self._sender(method, url, raise_for_status=raise_for_status, **kw)
        except httpx.HTTPStatusError as e:
            raise HttpError(self.name, e.response.status_code, e.response.reason_phrase,
                            e.response.text) from e
        except httpx.HTTPError as e:  # transport, timeout, decode
            raise Unavailable(f"[{self.name}] unreachable: {e}") from e

    # ``Backend.get`` is already the "one item by key" method, so the verbs are fetch_*.

    def fetch(self, url: str, **kw) -> httpx.Response:
        return self.request("GET", url, **kw)

    def fetch_json(self, url: str, **kw) -> Any:
        return self.fetch(url, **kw).json()

    def fetch_text(self, url: str, **kw) -> str:
        return self.fetch(url, **kw).text

    def fetch_bytes(self, url: str, **kw) -> bytes:
        return self.fetch(url, **kw).content

    def fetch_status(self, url: str, **kw) -> int:
        """GET ``url`` and return only the status, streaming the body away."""
        self._throttle()
        try:
            with self.http.stream("GET", url, **kw) as response:
                return response.status_code
        except httpx.HTTPError as e:
            raise Unavailable(f"[{self.name}] unreachable: {e}") from e
