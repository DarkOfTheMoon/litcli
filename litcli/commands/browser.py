"""`litcli browser login` / `litcli browser status` — the human end of the shared session.

Signing in once in the one shared profile is what replaces copying cookies per site. ``login``
opens the site in that profile and leaves the browser running; ``status`` reads each site's
answer and says whether the live session reaches it.
"""
from __future__ import annotations

import contextlib
import sys

from ..backends._cf_gate import is_challenge_page, is_sign_in_page, is_waf_page
from ..browser import BrowserUnavailable, Session

#: Where each publisher's sign-in leaves the session usable. The probe is the site root: it is
#: served to every visitor, so its answer tells entitlement state without naming an article.
SITES = {
    "sciencedirect": "https://www.sciencedirect.com/",
    "wiley": "https://onlinelibrary.wiley.com/",
    "springer": "https://link.springer.com/",
    "cambridge": "https://www.cambridge.org/core",
    "aip": "https://pubs.aip.org/",
    "acm": "https://dl.acm.org/",
    "aps": "https://link.aps.org/",
    "iop": "https://iopscience.iop.org/",
    "tandf": "https://www.tandfonline.com/",
    "aiaa": "https://arc.aiaa.org/",
    "ieee": "https://ieeexplore.ieee.org/",
}

_SIGNED_IN = "signed in"
_SIGNED_OUT = "signed out"
_GATED = "gated"


def _classify(status: int, body: str) -> str:
    """How the site answered: behind its bot gate, at a sign-in wall, or open."""
    if is_waf_page(body, status) or is_challenge_page(body, status):
        return _GATED
    title = ""
    marker = "<title>"
    if marker in body:
        title = body.split(marker, 1)[1].split("</title>", 1)[0]
    return _SIGNED_OUT if is_sign_in_page(body, title) else _SIGNED_IN


def cmd_browser_login(cfg, args) -> int:
    """Open ``--site`` in the shared profile and wait until the user is done signing in."""
    site = getattr(args, "site", None)
    if not site:
        print(f"[error] name a site: {', '.join(SITES)}", file=sys.stderr)
        return 2
    if site not in SITES:
        print(f"[error] unknown site {site}; choose one of: {', '.join(SITES)}",
              file=sys.stderr)
        return 2
    session = Session()
    try:
        session.start()
        session.page(SITES[site])
    except BrowserUnavailable as e:
        print(f"[error] {e}", file=sys.stderr)
        return 2
    print(f"Sign in to {site} in the window that just opened, then press Enter.")
    print("The browser stays up: later commands reuse this session, and it renews itself.")
    with contextlib.suppress(EOFError):
        input()
    return 0


def cmd_browser_status(cfg, args) -> int:
    """Report, per site, whether the live session gets through — and its gates."""
    session = Session()
    started = not session.alive()
    try:
        if started:
            session.start()
        rows = []
        for site, url in SITES.items():
            try:
                answer = session.page(url)
                rows.append((site, _classify(answer.status, answer.text)))
            except Exception as e:  # a single dead site must not hide the others
                rows.append((site, f"error: {e}"))
    except BrowserUnavailable as e:
        print(f"[error] {e}", file=sys.stderr)
        return 2
    finally:
        if started:
            session.close()
    width = max(len(site) for site, _ in rows)
    for site, state in rows:
        print(f"{site:<{width}}  {state}")
    print("\n`gated` means the site answered with its bot gate; `signed out` means no session. "
          "Run `litcli browser login <site>` for either.")
    return 0
