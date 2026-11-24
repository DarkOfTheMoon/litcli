"""Helpers shared by the entitled publisher readers (ScienceDirect, Wiley, SpringerLink,
Cambridge, AIP, ACM, APS, IOP, Taylor & Francis, AIAA, IEEE).

These sites serve the article only to a session the user entitled in their browser, and the
gated ones answer a plain HTTP client with a challenge (Cloudflare managed, or AWS WAF) instead
of the article. The pieces are therefore the same everywhere:

* the markers that identify a challenged page (``is_challenge_page``);
* reading a ``Cookie`` header or a Netscape ``cookies.txt`` export, keeping only the
  publisher's own hosts (``parse_cookie`` / ``load_cookie``);
* enforcing that a cookie arrives with its browser's ``user_agent`` — the clearance cookie is
  bound to the UA and the egress IP that earned it (``session_cookie``).

Everything here is pure and offline-testable.
"""
from __future__ import annotations

import re

#: Markers on the pages a Cloudflare managed challenge / a site's own bot block serves:
#: the challenge title and script scaffolding, the older "enable JavaScript" body, and the
#: template fallback blocks sites embed (ScienceDirect's ``CPE00001``). ``challenge-platform``
#: is *not* listed on its own: Cloudflare injects that script into every served page too.
CHALLENGE_MARKERS = ("CPE00001", "cf-mitigated", "Enable JavaScript and cookies to continue",
                     "Are you a robot", "__cf_chl", "Just a moment...", "gokuProps",
                     "awsWafCookieDomainList")


def is_challenge_page(page_html: str, status: int = 200) -> bool:
    """True when the body is a bot/entitlement gate, or the status is a plain 403."""
    if status == 403:
        return True
    return any(marker in (page_html or "") for marker in CHALLENGE_MARKERS)


def parse_cookie(raw: str, hosts: tuple[str, ...]) -> dict[str, str]:
    """Parse a browser ``Cookie`` header (``k=v; k2=v2``) into an ordered dict.

    Tolerates a leading ``Cookie:`` and lines of a Netscape ``cookies.txt`` export (only rows
    whose host is one of ``hosts`` or a subdomain of one); blank lines and ``#`` comments are
    skipped — except a ``#HttpOnly_`` row, which is the prefix a browser's export puts on
    exactly the cookies that matter here (``cf_clearance``, ``aws-waf-token``).

    A row for one of ``hosts`` verbatim outranks a row for a sibling subdomain, and a row for a
    deeper host outranks its parent zone's, whatever their order in the file: a clearance cookie
    is issued per exact hostname, so neither a ``journals.…`` row nor an ``aps.org`` one may
    shadow the ``link.aps.org`` row the request is actually going to.
    """
    found: dict[str, str] = {}
    rows: list[tuple[str, str, str]] = []
    for line in (raw or "").splitlines():
        line = line.strip()
        httponly = False
        if line.startswith("#HttpOnly_"):
            # A marker row is a cookie row, never a comment: only the marker is dropped.
            line = line[len("#HttpOnly_"):]
            httponly = True
        elif not line or line.startswith("#"):
            continue
        if line.lower().startswith("cookie:"):
            line = line[len("cookie:"):]
        # Netscape: domain, include_subdomains, path, secure, expiry, k, v. A marker row is
        # read this way even without a tab (some exports write spaces), and still falls back
        # to the inline pair shape if it has too few columns to be a row.
        columns = line.split("\t") if "\t" in line else (line.split() if httponly else [])
        if len(columns) >= 7:
            domain = columns[0].strip().lower().lstrip(".")
            if domain in hosts or any(domain.endswith("." + h) for h in hosts):
                rows.append((domain, columns[5].strip(), columns[6].strip()))
            continue
        for pair in line.split(";"):
            key, sep, value = pair.partition("=")
            if sep and key.strip():
                found.setdefault(key.strip(), value.strip())
    # Most specific first: a clearance cookie is per hostname, so the ``link.…`` row must
    # outrank its parent zone's row (and the exact host outranks a sibling subdomain).
    for _domain, key, value in sorted(
            rows, key=lambda r: (-r[0].count("."), 0 if r[0] in hosts else 1)):
        found.setdefault(key, value)
    return found


def load_cookie(cookie: str = "", cookie_file: str = "", hosts: tuple[str, ...] = (),
                name: str = "") -> str:
    """The cookie string from an inline string and/or a cookie file (both merged)."""
    merged = parse_cookie(cookie, hosts)
    if cookie_file:
        try:
            with open(cookie_file, encoding="utf-8", errors="replace") as f:
                merged.update({k: v for k, v in parse_cookie(f.read(), hosts).items()
                               if k not in merged})
        except OSError as e:
            raise RuntimeError(f"[{name}] cannot read cookie_file {cookie_file}: {e}") from e
    return "; ".join(f"{k}={v}" for k, v in merged.items())


def session_cookie(cookie: str, cookie_file: str, user_agent: str, hosts: tuple[str, ...],
                   name: str, gate: str = "Cloudflare's clearance cookie") -> str:
    """Build the cookie string, refusing a cookie that arrives without its user_agent.

    The string is not sent as a ``Cookie`` header (httpx drops one on every redirect): the
    reader plants it in its own hosts' jar, so it survives the ``doi.org`` hop and never
    reaches the wrong site.

    The entitlement cookie is bound to the UA (and the egress IP) that earned it, so a cookie
    alone can only ever be useless — say so at construction instead of a mystery 403. ``gate``
    names the cookie in that message (a site without Cloudflare names its own).
    """
    value = load_cookie(cookie, cookie_file, hosts, name)
    if value and not user_agent:
        raise RuntimeError(
            f"[{name}] a session cookie needs the browser's user_agent as well: {gate} is "
            "bound to the User-Agent (and the egress IP) that earned it, so both must be "
            "copied from the same browser")
    return value


#: A ``citation_*`` meta name: what every real item page carries and a sign-in wall does not.
_CITATION_META_RE = re.compile(r"""name=["']citation_""", re.I)
#: Title prefixes of a sign-in/interstitial page served instead of the item.
_SIGN_IN_TITLE_RE = re.compile(
    r"^\s*(?:sign|log)[ -]?in\b|^\s*(?:access|institutional) (?:through|access)\b", re.I)


def is_sign_in_page(page_html: str, title: str) -> bool:
    """True when the page is the site's sign-in wall instead of the item.

    A served article page also links "Sign in" in its navigation, so the title alone is not
    enough: the page must carry none of the ``citation_*`` identity meta every real item has.
    """
    return bool(title) and bool(_SIGN_IN_TITLE_RE.match(title)) \
        and not _CITATION_META_RE.search(page_html or "")


def gate_message(site: str, name: str) -> str:
    """The one message every gated reader raises when the gate meets it."""
    return (f"{site} served its bot/entitlement gate instead of the article "
            "(CPE00001 / Cloudflare challenge): open the article once in an authenticated "
            f"browser tab on your off-campus session, then copy that session's cookies into "
            f"[backends.{name}] cookie / cookie_file — with the browser's own user_agent, and "
            "from the same machine: the clearance cookie is bound to both the UA and the "
            "egress IP and the server may drop it at any time (or retry from an entitled "
            "network)")


def waf_gate_message(site: str, name: str) -> str:
    """The message an AWS WAF challenge raises (the IEEE shape of :func:`gate_message`)."""
    return (f"{site} served its AWS WAF challenge instead of the article: open the article once "
            f"in an authenticated browser tab on your off-campus session, then copy that "
            f"session's cookies into [backends.{name}] cookie / cookie_file — with the "
            "browser's own user_agent, and from the same machine: the aws-waf-token cookie is "
            "bound to both the UA and the egress IP and the server may drop it at any time "
            "(or retry from an entitled network)")
