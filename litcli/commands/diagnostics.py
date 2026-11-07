"""Diagnostics: shell completions and the ``backends`` subcommands."""
from __future__ import annotations

import contextlib
import os
import subprocess
import sys

from .. import config as config_mod
from ..completions import build_spec
from ..completions import render as render_completion
from ..config import get_all_backends, get_backend, write_backend_key
from ..errors import LitcliError
from ..paths import apply_browser_cache
from .parser import build_parser


def cmd_completions(cfg, args) -> int:
    """Generate a self-contained shell completion script for ``args.shell``.

    Introspects the live parser (so the spec tracks the current CLI surface) and prints the
    rendered script to stdout; the user sources it via ``eval "$(litcli completions bash)"``.

    Newlines are pinned to ``\\n``: a Windows text stream would otherwise write CRLF, and the
    ``bash``/``zsh`` engines fail on the stray carriage returns.
    """
    script = render_completion(args.shell, build_spec(build_parser()))
    with contextlib.suppress(AttributeError, ValueError):
        sys.stdout.reconfigure(newline="\n")  # captured streams take the text as-is
    sys.stdout.write(script)
    return 0


def _cfg_write_key(cfg: dict, name: str) -> str | None:
    """Get a backend's api_key from config (only checks presence, never prints it)."""
    bcfg = (cfg.get("backends") or {}).get(name) or {}
    k = bcfg.get("api_key")
    return k if k else None


def cmd_backends_status(cfg, args) -> int:
    """Per-backend read-only diagnostics: config / capabilities / authorization & reachability
    (never prints key plaintext)."""
    backends = get_all_backends(cfg)
    if not backends and not config_mod.failed_backends:
        print("[no backends] check ~/.config/litcli/config.toml (or env LITCLI_CONFIG)")
        return 0
    print(f"backend diagnostics: {len(backends)} configured")
    for name, b in sorted(backends.items()):
        caps = ", ".join(sorted(b.capabilities))
        has_write = "write" in b.capabilities
        if has_write:
            auth = ("authorized (has write key)" if _cfg_write_key(cfg, name)
                    else "unauthorized (write needs `backends authorize`)")
        else:
            auth = "read-only backend, no auth needed"
        # Lightweight reachability probe only for backends providing ping() (zotero local); non-fatal if it fails.
        probe = ""
        ping = getattr(b, "ping", None)
        if callable(ping):
            probe = "  reachable" if ping() else "  [unreachable: backend not running?]"
        print(f"  {name:<12} caps=[{caps}]  {auth}{probe}")
    for name, why in sorted(config_mod.failed_backends.items()):
        if name not in backends:
            print(f"  {name:<12} [unavailable] {why}")
    return 0


def cmd_backends_authorize(cfg, args) -> int:
    """Actively authorize against local Zotero to get a key, and write it back to config.toml.

    The authorized backend must support local authorization (implement authorize()). Once
    successful (or already authorized), writes no longer prompt.
    """
    backend = getattr(args, "backend", "zotero")
    try:
        b = get_backend(backend, cfg)
    except KeyError as e:
        print(f"[error] {e}")
        return 2
    if not hasattr(b, "authorize"):
        print(f"[error] backend '{backend}' does not support local authorization (only zotero_local provides it)")
        return 2
    try:
        key = b.authorize()
    except (LitcliError, RuntimeError) as e:
        print(f"[error] {e}")
        return 2
    if not key:
        print(f"[error] {backend} authorization returned no key (built-in key may be in use, or not approved)")
        return 2
    path = write_backend_key(backend, key)
    print(f"[ok] Wrote {backend}'s write key to {path}")
    print("     Subsequent writes (data add / set / del / attach) need no further authorization.")
    return 0


def _browser_launch_check() -> int:
    """Launch Chromium headless and report its version.

    Playwright's cache can hold a half-extracted or wrong-versioned tree that ``install``
    happily accepts; only an actual launch proves the download is usable (this is what the
    binary CI job asserts after installing).
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[error] this build has no playwright; install the wheel/source build, or "
              "`pip install playwright` into the same interpreter")
        return 2
    apply_browser_cache()  # a frozen driver would otherwise look inside the bundle
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                version = browser.version
            finally:
                browser.close()
    except Exception as e:  # missing browser, broken tree, no display
        print(f"[error] chromium does not launch: {e}")
        print("        run `litcli backends install-browser` first")
        return 2
    print(f"[ok] chromium {version} launches")
    return 0


def cmd_backends_install_browser(cfg, args) -> int:
    """Download the browser the cnki/douban backends drive, via the bundled Playwright driver.

    Kept out of the installer's hard payload on purpose: the browser is ~150 MB and only two
    backends need it. The browser lands in Playwright's own cache (``~/.cache/ms-playwright``
    or ``%LOCALAPPDATA%\\ms-playwright``), so installing a new litcli build keeps it.
    ``--check`` skips the download and launches the browser instead.
    """
    if getattr(args, "check", False):
        return _browser_launch_check()
    browser = getattr(args, "browser", "chromium") or "chromium"
    try:
        from playwright._impl._driver import compute_driver_executable
    except ImportError:
        print("[error] this build has no playwright; install the wheel/source build, or "
              "`pip install playwright` into the same interpreter")
        return 2
    driver = compute_driver_executable()
    # Playwright >= 1.40 returns (node, cli.js); older ones a single runnable path.
    cmd = [str(part) for part in driver] if isinstance(driver, tuple) else ["node", str(driver)]
    download = [*cmd, "install"]
    if getattr(args, "with_deps", False):
        download.append("--with-deps")
    if getattr(args, "dry_run", False):
        download.append("--dry-run")
    # The driver is a separate process: give it the same cache the launch path uses (frozen
    # Playwright defaults the driver it starts to "0", browsers inside the wiped bundle).
    env = {**os.environ, "PLAYWRIGHT_BROWSERS_PATH": str(apply_browser_cache())}
    print(f"downloading {browser}; this can take a few minutes and ~150 MB …")
    rc = subprocess.call([*download, browser], env=env)
    if rc != 0:
        print(f"[error] browser download failed (playwright exit {rc})")
        return 2
    print(f"{browser} installed; cnki/douban can run now")
    return 0


_SUBCOMMANDS = {"status": cmd_backends_status, "authorize": cmd_backends_authorize,
                "install-browser": cmd_backends_install_browser}


def cmd_backends(cfg, args) -> int:
    subcmd = getattr(args, "backends_cmd", None)
    if subcmd in _SUBCOMMANDS:
        return _SUBCOMMANDS[subcmd](cfg, args)
    # Bare `backends` or `backends list`: list configured backends with capabilities (read-only)
    backends = get_all_backends(cfg)
    for name, b in sorted(backends.items()):
        caps = ", ".join(sorted(b.capabilities))
        print(f"{name:<12} caps=[{caps}]")
    for name, why in sorted(config_mod.failed_backends.items()):
        if name not in backends:
            print(f"{name:<12} [unavailable] {why}")
    return 0
