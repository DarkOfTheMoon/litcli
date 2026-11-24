"""The shared shape of the entitled publisher readers.

Every publisher reader here serves an article only to a session the user entitled in their
own browser, so they share everything around that: the cookie-plus-``user_agent`` pair
(the entitlement is bound to the UA that earned it), the read-only capability, the "not a local
attachment" contract, and the one message a gate turns into.

What stays with a subclass is only what is actually site-specific: its URLs, its key
normalization, its parsers, and the wording of its gate. ``_open`` is the single network
entry point a page helper needs — a challenge (in the status, the body, or a
``cf-mitigated`` header) becomes :class:`BotWall` carrying :attr:`BLOCK_MSG` instead of a
mystery 403. The page memo, the identifier-only ``search`` and the "why was the PDF refused"
reason are equally shared. A site whose key space or PDF route differs (sciencedirect's PII API,
wiley's TDM path, springer's landing probe) overrides ``normalize_key``/``_page``/``get``/
``get_attachment`` and says in each docstring which inherited hook it replaces and why.
"""
from __future__ import annotations

from typing import ClassVar
from urllib.parse import urlparse

from ..errors import BadInput, BotWall, NotFound, Paywalled, Unavailable
from ..models import AcademicItem
from ..transport import HttpError
from ._cf_gate import is_challenge_page, is_sign_in_page, session_cookie
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
    #: ``cf`` (Cloudflare managed challenge) or ``waf`` (AWS WAF challenge).
    GATE_KIND = "cf"

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
        #: Fetched pages, per normalized key: a read and its PDF download share one request
        #: without a second key evicting the first. The value is whatever the site's ``_page``
        #: stores (an HTML body, or a body/landed-URL pair).
        self._memo: dict[str, object] = {}

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

    # -- page memo -------------------------------------------------------------

    def _memo_get(self, key: str):
        """The page already fetched under ``key``, or ``None``."""
        return self._memo.get(key)

    def _memo_put(self, key: str, value) -> None:
        self._memo[key] = value

    # -- gate detection --------------------------------------------------------

    def _is_gate(self, body: str, status: int) -> bool:
        if self.GATE_KIND == "waf":
            # The WAF answers its JS challenge as an empty 202 carrying ``gokuProps``; the site
            # never serves a real article on 202.
            return status in (202, 403) or "gokuProps" in (body or "")
        return is_challenge_page(body, status)

    def _open(self, url: str, **kw):
        """GET ``url``; a gate in the status, the body or a header becomes :class:`BotWall`.

        The plain raise-for-status is kept, so a 5xx still reaches the retry path.
        """
        try:
            response = self.fetch(url, **kw)
        except HttpError as e:
            if self._is_gate(e.body, e.status):
                raise BotWall(self.BLOCK_MSG) from e
            raise
        if self._is_gate(response.text, response.status_code) \
                or (self.GATE_KIND == "cf" and response.headers.get("cf-mitigated")):
            raise BotWall(self.BLOCK_MSG)
        return response

    # -- item shape ------------------------------------------------------------

    def is_open_access(self, page_html: str) -> bool:
        """True when the page carries the site's open-access licence block (default: never)."""
        return False

    def _short_page(self, url: str) -> bool:
        """True for a paywalled short page (a full page means OA or an entitled session)."""
        return False

    def _refusal_reason(self, page: str, landed: str, response) -> str:
        """Why the PDF endpoint answered something that is not a PDF."""
        # A licence block seen on the item's *short* page may belong to a related-item card,
        # so there the entitlement checks below are the honest answer, not the OA claim.
        if self.is_open_access(page) and not self._short_page(landed):
            return "the item is open access but the endpoint answered an HTML page"
        if not self.has_session:
            return "the session holds no clearance cookie"
        if self._short_page(landed):
            return ("the item answers only its short page, so the session is not entitled to it "
                    "(missing, expired, or not subscribed)")
        return "the session is not entitled to this item (expired, or not subscribed)"

    def _guard_sign_in(self, page: str, landed: str, title: str) -> None:
        """Raise :class:`BotWall` when the answer is the site's sign-in page, not an item.

        A 200 login page otherwise parses as an article titled "Sign in to …" with no
        attachment, which the chain reads as "no copy here" instead of "configure a session".
        """
        if is_sign_in_page(page, title):
            raise BotWall(
                f"[{self.name}] the site served its sign-in page instead of the item: {landed} "
                "— the session is not signed in (copy the cookies of an authenticated browser "
                f"tab into [backends.{self.name}] cookie / cookie_file, with that browser's "
                "user_agent)")

    # -- Backend ---------------------------------------------------------------

    def search(self, query: str, limit: int = 10, filters: dict | None = None) -> list[AcademicItem]:
        """Resolve a DOI / URL query to one item; keyword queries return [].

        There is no key-free search endpoint on these sites, so anything that is not already an
        identifier cannot be looked up here. ``filters`` are not applied: there is at most one
        hit.
        """
        try:
            self.normalize_key(query or "")
        except BadInput:
            return []
        if limit < 1:
            return []
        try:
            return [self.get(query)]
        except (NotFound, KeyError):
            return []

    def _gated_download(self, url: str, page: str, landed: str) -> bytes:
        """Fetch the PDF bytes, turning a gate into :class:`Unavailable` and a non-PDF into
        :class:`Paywalled` via :meth:`_refusal_reason`.

        ``page`` is the memoized item page and ``landed`` its own landed URL; ``url`` is the
        PDF endpoint, whose path is what the message points the reader at.
        """
        try:
            response = self.fetch(url, headers={"Accept": "application/pdf"})
        except HttpError as e:
            if self._is_gate(e.body, e.status):
                raise Unavailable(f"[{self.name}] PDF download was gated: {self.BLOCK_MSG}") from e
            raise
        if response.content.startswith(b"%PDF"):
            return response.content
        if self._is_gate(response.text, response.status_code):
            # Cloudflare serves its interstitial with a 200 too: that is the gate, not a refusal.
            raise Unavailable(f"[{self.name}] PDF download was gated: {self.BLOCK_MSG}")
        landed_on = urlparse(str(response.url)).path or url
        why = self._refusal_reason(page, landed, response)
        raise Paywalled(
            f"[{self.name}] no PDF bytes: {why} (landed on {landed_on}) — open {url} in your "
            "entitled browser, or refresh the cookie+user_agent pair from a cleared tab")
