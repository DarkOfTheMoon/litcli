"""Helpers shared by the entitled publisher readers (ScienceDirect, Wiley, SpringerLink).

These sites serve the article only to a session the user entitled in their browser, and the
two Cloudflare-walled ones answer a plain HTTP client with a managed challenge instead of the
article. The pieces are therefore the same everywhere:

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
                     "Are you a robot", "__cf_chl", "Just a moment...")


def is_challenge_page(page_html: str, status: int = 200) -> bool:
    """True when the body is a bot/entitlement gate, or the status is a plain 403."""
    if status == 403:
        return True
    return any(marker in (page_html or "") for marker in CHALLENGE_MARKERS)


def parse_cookie(raw: str, hosts: tuple[str, ...]) -> dict[str, str]:
    """Parse a browser ``Cookie`` header (``k=v; k2=v2``) into an ordered dict.

    Tolerates a leading ``Cookie:`` and lines of a Netscape ``cookies.txt`` export (only rows
    whose host contains one of ``hosts``); blank lines and ``#`` comments are skipped.
    """
    found: dict[str, str] = {}
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("cookie:"):
            line = line[len("cookie:"):]
        if "\t" in line:  # Netscape: domain, include_subdomains, path, secure, expiry, k, v
            parts = line.split("\t")
            host = parts[0].lower()
            if len(parts) >= 7 and any(h in host for h in hosts):
                found.setdefault(parts[5].strip(), parts[6].strip())
            continue
        for pair in line.split(";"):
            key, sep, value = pair.partition("=")
            if sep and key.strip():
                found.setdefault(key.strip(), value.strip())
    return found


def load_cookie(cookie: str = "", cookie_file: str = "", hosts: tuple[str, ...] = (),
                name: str = "") -> str:
    """The ``Cookie`` header value from an inline string and/or a cookie file (both merged)."""
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
    """Build the ``Cookie`` header, refusing a cookie that arrives without its user_agent.

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
