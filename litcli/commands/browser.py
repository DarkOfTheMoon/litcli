"""`litcli browser login` / `status` / `stop` — the human end of the shared session.

Signing in once in the one shared profile is what replaces copying cookies per site. ``login``
opens the site in that profile and leaves the browser running; ``status`` reads each site's
answer and says whether the live session reaches it; ``stop`` ends the session and leaves
whatever browser the user would still want.
"""
from __future__ import annotations

import contextlib
import shutil
import subprocess
import sys
import time

from .. import browser_broker as broker
from .. import paths
from ..backends._cf_gate import is_challenge_page, is_sign_in_page, is_waf_page
from ..browser import (
    BrowserUnavailable,
    PlaywrightTimeoutError,
    Session,
    session_from_config,
)

#: How long ``login`` waits for other leases on a headless broker before giving up.
_WAIT_FOR_IDLE = 30.0

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


def _session(cfg: dict, *, pinned: bool = False) -> Session:
    """The session these two commands open: the configured one, enabled or not.

    ``browser login`` is how the session gets set up, so it cannot depend on the switch that
    makes ``fetch`` use it — but both have to agree on profile, proxy and headless or the
    sign-in would land in a browser nothing else looks at.
    """
    section = dict(cfg.get("browser") or {})
    section["enabled"] = True
    return session_from_config({**cfg, "browser": section}, pinned=pinned)


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
    if _targets_the_users_own_profile(cfg):
        return _login_in_window(cfg, site)
    session = _session(cfg, pinned=True)
    try:
        session.start()
        headless = bool(session._lease and session._lease.headless)
        if session.mode != "attached" and headless:
            # A hidden browser has no window to sign in through. One nobody else holds can be
            # replaced by a visible one; one in use has to be waited for.
            if not _wait_released(session._lease.control_port):
                print("[error] someone is using the browser, try again later", file=sys.stderr)
                return 2
            session.terminate()
            session = _session(cfg, pinned=True)
            session._headless = False
            session.start()
        if session.mode == "launched":
            broker.pin(session._lease.control_port, pinned=True)  # no idle exit mid sign-in
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


def _targets_the_users_own_profile(cfg: dict) -> bool:
    """Whether ``[browser].profile`` resolves to the platform's default Chrome profile."""
    return (paths.resolve_browser_profile((cfg.get("browser") or {}).get("profile"))
            == paths.user_browser_profile_dir())


def _open_window(cfg: dict, url: str) -> None:
    """Start a Chrome on the user's own profile, detached, with ``url`` already open.

    No debugging port is asked for, and none is reported: Chrome will not open one while
    ``--user-data-dir`` names its default data directory, which is what the user's profile is.
    The sign-ins still land in that profile, which is all the headless reader needs.
    """
    section = cfg.get("browser") or {}
    binary = broker._binary(section.get("channel", "chrome"))
    argv = [binary]
    if not any(shutil.which(name) for name in broker._CHROME_BINARIES):
        # The bundled Chromium has no default store of its own here: name the profile or the
        # sign-ins would land in a browser nothing else opens.
        argv.append(f"--user-data-dir={paths.resolve_browser_profile(section.get('profile'))}")
    if section.get("profile_directory"):
        argv.append(f"--profile-directory={section['profile_directory']}")
    proxy = (cfg.get("network") or {}).get("proxy")
    if proxy:
        argv.append(f"--proxy-server={proxy}")
    argv += ["--no-first-run", "--no-default-browser-check", url]
    try:
        subprocess.Popen(argv, start_new_session=True, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    except OSError as e:
        raise BrowserUnavailable(f"could not start {binary}: {e}",
                                 hint="litcli backends install-browser") from e


def _login_in_window(cfg: dict, site: str) -> int:
    """The own-profile path: a plain window, no session, no probing."""
    try:
        _open_window(cfg, SITES[site])
    except BrowserUnavailable as e:
        print(f"[error] {e}", file=sys.stderr)
        return 2
    print(f"Sign in to {site} in the window that just opened.")
    print("Then press Enter here, in the terminal (not in the browser window) — that is what "
          "ends this command.")
    print("The sign-in stays in your browser profile: later commands read it from there.")
    with contextlib.suppress(EOFError):
        input()
    return 0


def _wait_released(control_port: int) -> bool:
    """Wait for every other lease on the broker to go, up to :data:`_WAIT_FOR_IDLE`."""
    deadline = time.monotonic() + _WAIT_FOR_IDLE
    while True:
        try:
            if not broker.status(control_port).get("leases"):
                return True
        except (broker.BrokerUnavailable, OSError, ValueError):
            return True  # no broker left to wait for
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.25)


def cmd_browser_stop(cfg, args) -> int:
    """Stop the broker; what happens to the browser is decided by who owns it.

    A browser the user opened is theirs and is left running; one we started headless is
    invisible and goes; one we started with a window is closed by closing it.
    """
    state = broker.read_session()
    if not state:
        print("no session")
        return 0
    try:
        lease = broker.connect(state["control_port"], headless=False)
    except broker.BrokerUnavailable:
        print("no session")
        return 0
    owns, headless, pid = bool(state.get("owns")), bool(state.get("headless")), state.get("pid")
    try:
        with contextlib.suppress(BrowserUnavailable, OSError, ValueError):
            broker.shutdown(lease.control_port, force=True)  # gone already is the same outcome
    finally:
        lease.close()
    if not owns:
        print("your Chrome was left running")
    elif headless:
        print(f"stopped (pid {pid})")
    else:
        print(f"left running (pid {pid}) - close the window to end it")
    return 0


def cmd_browser_status(cfg, args) -> int:
    """Report, per site, whether the live session gets through — and its gates."""
    session = _session(cfg)
    try:
        if not session.alive():
            session.start()
        info = broker.status(session._lease.control_port) if session._lease else {}
        for name in ("profile", "source", "profile_directory", "mode", "leases"):
            value = info.get(name)
            if name == "mode":
                value = session.mode
            print(f"{name}: {value}")
        print()
        rows = [(site, _probe(session, url)) for site, url in SITES.items()]
    except BrowserUnavailable as e:
        print(f"[error] {e}", file=sys.stderr)
        return 2
    finally:
        # Also when the browser was already running and only attached here: the attach leaves a
        # Playwright driver active in this process, and the next session start would collide.
        # The broker keeps running either way — it idles out on its own.
        session.close()
    width = max(len(site) for site, _ in rows)
    for site, state in rows:
        print(f"{site:<{width}}  {state}")
    print("\n`gated` means the site answered with its bot gate; `signed out` means no session; "
          "`no answer` means it did not finish loading in time. Run `litcli browser login "
          "<site>` for the first two.")
    return 0
