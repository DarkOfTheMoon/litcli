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

    A row for one of ``hosts`` verbatim outranks a row for a sibling subdomain, whatever their
    order in the file: a clearance cookie is issued per exact hostname, so a ``journals.…`` row
    must not shadow the ``link.…`` one the request is actually going to.
    """
    found: dict[str, str] = {}
    rows: list[tuple[str, str, str]] = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        elif not line or line.startswith("#"):
            continue
        if line.lower().startswith("cookie:"):
            line = line[len("cookie:"):]
        if "\t" in line:  # Netscape: domain, include_subdomains, path, secure, expiry, k, v
            parts = line.split("\t")
            domain = parts[0].strip().lower().lstrip(".")
            if len(parts) >= 7 and (
                    domain in hosts or any(domain.endswith("." + h) for h in hosts)):
                rows.append((domain, parts[5].strip(), parts[6].strip()))
            continue
        for pair in line.split(";"):
            key, sep, value = pair.partition("=")
            if sep and key.strip():
                found.setdefault(key.strip(), value.strip())
    for _domain, key, value in sorted(rows, key=lambda r: 0 if r[0] in hosts else 1):
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


def gate_message(site: str, name: str) -> str:
    """The one message both readers raise when the gate meets them."""
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
