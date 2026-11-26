"""The backend base class for HTTP sources.

On top of :mod:`litcli.transport` (client, proxy, ``HttpError``) this adds what a network
backend needs and a one-off request does not: a pooled lazily built client, retries through
``tenacity`` and a per-site rate through ``pyrate-limiter``. Backends keep only their URLs,
their parsers and their key normalization.

Failures surface as typed :class:`~litcli.errors.LitcliError` subclasses with a ``[backend]``
prefix, which the CLI turns into an error line or a ``[skipped]`` backend instead of a stack trace.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any, ClassVar

import httpx
from pyrate_limiter import Duration, Limiter, Rate
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from .. import cache, debug
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


#: Longest ``Retry-After`` we will sit through before the next attempt.
RETRY_AFTER_CAP = 30.0


def retry_after_seconds(exc: BaseException, cap: float = RETRY_AFTER_CAP) -> float | None:
    """The server's own ``Retry-After`` (seconds), when it sent one.

    The exponential schedule starts at half a second; rate limiters here (arXiv, OpenAlex) ask
    for tens of seconds, so ignoring the header means burning the attempts for nothing. A site
    whose penalty window is measured in minutes needs a higher ``cap`` than the default.
    """
    response = getattr(exc, "response", None)
    value = (response.headers.get("Retry-After") if response is not None else None) or ""
    try:
        return min(max(float(value.strip()), 0.0), cap)
    except ValueError:
        return None


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
    #: Longest ``Retry-After`` this backend will honour (a site with a minute-scale penalty
    #: window needs more than the default; the attempts themselves cap the total).
    retry_after_cap = RETRY_AFTER_CAP
    #: Status codes worth another attempt; backends that read a status as a page state drop it.
    retry_codes = RETRY_CODES
    #: Backoff between retries; tests swap in ``wait_none()``.
    retry_wait = wait_exponential(multiplier=0.5, max=8)
    follow_redirects = True
    http2 = False
    #: Metadata reads may be served from the local cache; page-state backends turn this off.
    cacheable = True

    def __init__(self, name: str | None = None, timeout: float | None = None,
                 rate: int | None = None, min_interval: float | None = None,
                 retries: int | None = None, retry_after_cap: float | None = None, **session_kw):
        if name is not None:
            self.name = name
        if timeout is not None:
            self.timeout = timeout
        if rate is not None:
            self.rate = rate
        if min_interval is not None:
            self.min_interval = min_interval
        # A per-site retry budget is a config matter: a fan-out source (OpenAlex) and a
        # single-item reader have very different tolerances, and which one a run is, is the
        # user's call rather than a class attribute.
        if retries is not None:
            self.retries = int(retries)
        if retry_after_cap is not None:
            self.retry_after_cap = float(retry_after_cap)
        super().__init__(timeout=self.timeout, ua=self.UA, headers=self.headers,
                         follow_redirects=self.follow_redirects, http2=self.http2, **session_kw)
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

    def _wait(self, state) -> float:
        """Backoff for the next attempt: the server's ``Retry-After`` when given, else the schedule."""
        exc = state.outcome.exception() if state.outcome else None
        asked = retry_after_seconds(exc, self.retry_after_cap) if exc is not None else None
        scheduled = self.retry_wait(state)
        return max(asked, scheduled) if asked is not None else scheduled

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
                wait=self._wait,
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

    def _cache_key(self, url: str, headers: dict | None = None) -> str:
        extra = "" if not headers else "|" + ";".join(f"{k}={v}" for k, v in sorted(headers.items()))
        return f"{self.name}|{url}|{self.headers.get('Accept', '')}|{self.UA}{extra}"

    def _cache_slot(self, url: str, kw: dict) -> str | None:
        if self.cacheable and cache.enabled():
            return self._cache_key(url, kw.get("headers"))
        return None

    def fetch_json(self, url: str, **kw) -> Any:
        """GET and parse JSON; only a body that really parses is cached.

        Caching before parsing meant a 200 WAF/error page stayed in the cache for a day and kept
        failing every later attempt, retries included.
        """
        key = self._cache_slot(url, kw)
        if key is not None:
            hit = cache.get(key)
            if hit is not None:
                try:
                    data = json.loads(hit)
                    cache.note(self.name, True)
                    log(f"cache hit {self.name}: {debug.mask(url)}")
                    return data
                except ValueError:
                    pass  # stale/garbled entry: take the network path
        body = self.fetch(url, **kw).text
        cache.note(self.name, False)
        data = json.loads(body)
        if key is not None:
            cache.put(key, body)
        return data

    def fetch_text(self, url: str, *, cache_read: bool = True, **kw) -> str:
        """GET as text, through the response cache when this backend allows it.

        The retry/pacing path is skipped on a hit, which is the point: a repeated search
        should not spend the site's budget again. ``cache_read=False`` fetches past a stored
        entry but still stores the fresh body over it (a page read to clear a bot check must
        not be answered from the stale one, and the real page has to replace it).
        """
        key = self._cache_slot(url, kw)
        if key is not None and cache_read:
            hit = cache.get(key)
            if hit is not None:
                cache.note(self.name, True)
                log(f"cache hit {self.name}: {debug.mask(url)}")
                return hit
        body = self.fetch(url, **kw).text
        cache.note(self.name, False)
        if key is not None:
            cache.put(key, body)
        return body

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
