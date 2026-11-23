"""The shared shape of the entitled publisher readers.

Every publisher reader here serves an article only to a session the user entitled in their
own browser, so they share everything around that: the cookie-plus-``user_agent`` pair
(the entitlement is bound to the UA that earned it), the read-only capability, the "not a local
attachment" contract, and the one message a gate turns into.

What stays with a subclass is only what is actually site-specific: its URLs, its key
normalization, its parsers, and the wording of its gate. ``_open`` is the single network
entry point a page helper needs — a challenge (in the status, the body, or a
``cf-mitigated`` header) becomes :attr:`BLOCK_MSG` instead of a mystery 403.
"""
from __future__ import annotations

from typing import ClassVar
from urllib.parse import urlparse

from ..transport import HttpError
from ._cf_gate import is_challenge_page, session_cookie
from .http import HttpBackend


class EntitledBackend(HttpBackend):
    """Base for a reader that needs the user's own entitled browser session."""

    access = "entitled"  # a subscription copy through the user's institution, not an OA licence
    #: The page is session/IP state, so it must not sit in the metadata cache.
    cacheable = False
    #: Hosts a cookie export may carry; a subclass sets its publisher's own (empty means
    #: :attr:`ALLOWED_HOSTS`).
    COOKIE_HOSTS: ClassVar[tuple[str, ...]] = ()
    #: Hosts a PDF link may legitimately live on; a subclass sets its publisher's own.
    ALLOWED_HOSTS: ClassVar[tuple[str, ...]] = ()
    #: How the cookie is named in the construction error (a site without Cloudflare says so).
    GATE_NAME = "the session cookie"
    #: The one message raised whenever a gate meets a request.
    BLOCK_MSG = ""

    def __init__(self, name: str = "", timeout: float | None = None,
                 delay: float | None = None, cookie: str = "", cookie_file: str = "",
                 user_agent: str = "", **_unused):
        # Fail early: a bad cookie_file, or a cookie without its UA, is a config error. A site
        # that sets only ALLOWED_HOSTS still keeps its rows instead of silently keeping none.
        hosts = self.COOKIE_HOSTS or self.ALLOWED_HOSTS
        self.cookie = session_cookie(cookie, cookie_file, user_agent, hosts, name,
                                     gate=self.GATE_NAME)
        if user_agent:
            # The entitlement is bound to the UA that earned it: send it verbatim.
            self.UA = user_agent.strip()
        # The cookie goes into the jar for the site's own hosts, not into a header: httpx drops
        # a header on every redirect, and every DOI key starts at ``doi.org``.
        pairs = dict(p.strip().partition("=")[::2] for p in self.cookie.split(";") if "=" in p)
        seeds: list[str] = []
        for host in hosts:  # the exact host only: a jar entry on its parent zone is sent twice
            if not any(h == host or h.endswith("." + host) for h in seeds):
                seeds.append(host)
        super().__init__(name=name, timeout=timeout, min_interval=delay, seed_cookies=pairs,
                         seed_hosts=seeds)

    @property
    def capabilities(self) -> set[str]:
        # Read-only, and deliberately not "attachment": the fulltext chain must treat a hit as
        # a remote source, never as a local-store attachment.
        return {"read"}

    def matches_url(self, url: str) -> bool:
        """True when ``url`` lives on this site (a hit found elsewhere may still need us)."""
        host = (urlparse(url).hostname or "").lower()
        return any(host == s or host.endswith("." + s) for s in self.ALLOWED_HOSTS)

    @property
    def has_session(self) -> bool:
        """Whether a session cookie was configured at all (the site still decides entitlement)."""
        return bool(self.cookie)

    def _open(self, url: str, **kw):
        """GET ``url``; a gate in the status, the body or a header becomes :attr:`BLOCK_MSG`.

        The plain raise-for-status is kept, so a 5xx still reaches the retry path.
        """
        try:
            response = self.fetch(url, **kw)
        except HttpError as e:
            if is_challenge_page(e.body, e.status):
                raise RuntimeError(self.BLOCK_MSG) from e
            raise
        if is_challenge_page(response.text, response.status_code) \
                or response.headers.get("cf-mitigated"):
            raise RuntimeError(self.BLOCK_MSG)
        return response
