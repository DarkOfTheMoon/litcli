"""Diagnostics: shell completions and the ``backends`` subcommands."""
from __future__ import annotations

import sys

from .. import config as config_mod
from ..completions import build_spec
from ..completions import render as render_completion
from ..config import get_all_backends, get_backend, write_backend_key
from .parser import build_parser


def cmd_completions(cfg, args) -> int:
    """Generate a self-contained shell completion script for ``args.shell``.

    Introspects the live parser (so the spec tracks the current CLI surface) and prints the
    rendered script to stdout; the user sources it via ``eval "$(litcli completions bash)"``.
    """
    sys.stdout.write(render_completion(args.shell, build_spec(build_parser())))
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
    except RuntimeError as e:
        print(f"[error] {e}")
        return 2
    if not key:
        print(f"[error] {backend} authorization returned no key (built-in key may be in use, or not approved)")
        return 2
    path = write_backend_key(backend, key)
    print(f"[ok] Wrote {backend}'s write key to {path}")
    print("     Subsequent writes (data add / set / del / attach) need no further authorization.")
    return 0


_SUBCOMMANDS = {"status": cmd_backends_status, "authorize": cmd_backends_authorize}


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
