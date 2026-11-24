"""`litcli browser login` / `litcli browser status` — the human end of the shared session.

Signing in once in the one shared profile is what replaces copying cookies per site. ``login``
opens the site in that profile and leaves the browser running; ``status`` reads each site's
answer and says whether the live session reaches it.
"""
from __future__ import annotations

import contextlib
import sys

from ..backends._cf_gate import is_challenge_page, is_sign_in_page, is_waf_page
from ..browser import BrowserUnavailable, PlaywrightTimeoutError, Session, session_from_config

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
_NO_ANSWER = "no answer"  # the site never finished loading; a slow site, not a refusal


def _classify(status: int, body: str) -> str:
    """How the site answered: behind its bot gate, at a sign-in wall, or open."""
    if is_waf_page(body, status) or is_challenge_page(body, status):
        return _GATED
    title = ""
    marker = "<title>"
    if marker in body:
        title = body.split(marker, 1)[1].split("</title>", 1)[0]
    return _SIGNED_OUT if is_sign_in_page(body, title) else _SIGNED_IN


def _probe(session: Session, url: str) -> str:
    """One site's answer. A timeout gets one retry: a probe is cheap, a slow site is not a no."""
    for _ in range(2):
        try:
            answer = session.page(url)
            return _classify(answer.status, answer.text)
        except PlaywrightTimeoutError:
            state = _NO_ANSWER
        except Exception as e:  # a single dead site must not hide the others; no retry
            # Only the first line: the message carries a multi-line Playwright call log,
            # which would split this site's row into several.
            return f"error: {str(e).splitlines()[0]}"
    return state


def _session(cfg: dict) -> Session:
    """The session these two commands open: the configured one, enabled or not.

    ``browser login`` is how the session gets set up, so it cannot depend on the switch that
    makes ``fetch`` use it — but both have to agree on profile, proxy and headless or the
    sign-in would land in a browser nothing else looks at.
    """
    section = dict(cfg.get("browser") or {})
    section["enabled"] = True
    return session_from_config({**cfg, "browser": section})


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
    session = _session(cfg)
    try:
        session.start()
        session.page(SITES[site])
    except BrowserUnavailable as e:
        print(f"[error] {e}", file=sys.stderr)
        return 2
    finally:
        # Disconnect before exiting. Left attached, interpreter exit tears the Playwright
        # driver down with its CDP socket still open and node throws an EPIPE traceback over
        # the prompt the user just got back. `close` only detaches — the browser stays up.
        session.close()
    print(f"Sign in to {site} in the window that just opened.")
    print("Then press Enter here, in the terminal (not in the browser window) — that is what "
          "ends this command.")
    print("The browser stays up: later commands reuse this session, and it renews itself.")
    with contextlib.suppress(EOFError):
        input()
    return 0


def cmd_browser_status(cfg, args) -> int:
    """Report, per site, whether the live session gets through — and its gates."""
    session = _session(cfg)
    started = not session.alive()
    try:
        if started:
            session.start()
        rows = [(site, _probe(session, url)) for site, url in SITES.items()]
    except BrowserUnavailable as e:
        print(f"[error] {e}", file=sys.stderr)
        return 2
    finally:
        # Also when the browser was already running and only attached here: the attach leaves a
        # Playwright driver active in this process, and the next session start would collide. A
        # browser this command started is stopped again — a probe is not worth leaving one for.
        if started:
            session.terminate()
        else:
            session.close()
    width = max(len(site) for site, _ in rows)
    for site, state in rows:
        print(f"{site:<{width}}  {state}")
    print("\n`gated` means the site answered with its bot gate; `signed out` means no session; "
          "`no answer` means it did not finish loading in time. Run `litcli browser login "
          "<site>` for the first two.")
    return 0
