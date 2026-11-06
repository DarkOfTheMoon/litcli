"""litcli entry point: build the argparse surface, apply the config, dispatch the command.

The commands live in ``litcli.commands``, one module per intent family; this module only
parses, sets up the process (config, proxy) and maps ``args`` onto them. Write commands
first check the backend's capability; read-only backends raise ``NotSupportedError``,
reported here as ``[skipped]``.
"""
from __future__ import annotations

import os
import sys

from .backends.base import NotSupportedError
from .commands import data as data_cmd
from .commands import diagnostics, export, fetch, graph, search
from .commands.parser import build_parser
from .config import load_config
from .transport import set_proxy

#: Top-level commands that take only ``(cfg, args)``; the graph edges and the two subcommand
#: families need their subcommand name as well and are dispatched separately.
_TOP_LEVEL_COMMANDS = {
    "search": search.cmd_search,
    "export": export.cmd_export,
    "completions": diagnostics.cmd_completions,
    "backends": diagnostics.cmd_backends,
}

_DATA_COMMANDS = {
    "get": data_cmd.cmd_data_get, "add": data_cmd.cmd_data_add,
    "set": data_cmd.cmd_data_set, "del": data_cmd.cmd_data_del,
    "attach": data_cmd.cmd_data_attach,
    "tree": data_cmd.cmd_data_tree, "members": data_cmd.cmd_data_members,
}


def _apply_proxy(cfg: dict) -> None:
    """Route traffic through the configured proxy unless an env var already set one.

    ``[network].proxy`` is for campus / VPN access to overseas sources; HTTP(S)_PROXY in the
    environment takes precedence when present.
    """
    prog = (cfg.get("network") or {}).get("proxy")
    if not prog:
        return
    if os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy"):
        return
    set_proxy(prog)


def _utf8_redirected_streams() -> None:
    """Force UTF-8 on redirected stdout/stderr under Windows.

    A Windows console handles Unicode itself, but a pipe or file is encoded with the active
    code page (cp936, cp1252), so a Chinese title in JSON output raises ``UnicodeEncodeError``.
    Undecodable console bytes become ``?`` instead of failing the command.
    """
    if os.name != "nt":
        return
    for stream in (sys.stdout, sys.stderr):
        try:
            if not stream.isatty():
                stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass  # replaced streams (tests, wrappers) need no help


def main(argv=None) -> int:
    _utf8_redirected_streams()
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config()
        _apply_proxy(cfg)
        if args.cmd in _TOP_LEVEL_COMMANDS:
            return _TOP_LEVEL_COMMANDS[args.cmd](cfg, args)
        if args.cmd in graph.EDGE_METHODS:
            return graph.cmd_edge(cfg, args, graph.EDGE_METHODS[args.cmd])
        if args.cmd == "data":
            return _DATA_COMMANDS[args.data_cmd](cfg, args)
        if args.cmd == "fetch":
            return fetch.cmd_fetch(cfg, args)
        print(f"[error] unknown command {args.cmd}")
        return 2
    except NotSupportedError as e:
        print(f"[skipped] {e}")
        return 2
    except KeyError as e:
        print(f"[error] {e}")
        return 2
    except (RuntimeError, ValueError, SyntaxError, OSError) as e:
        # Backend business errors (HTTP 401/404/timeout) and data-shape failures (non-JSON response,
        # malformed XML, config TOML syntax) are surfaced gracefully, never as a raw stack trace.
        # (json.JSONDecodeError -> ValueError, lxml XMLSyntaxError -> SyntaxError,
        #  TOMLDecodeError -> ValueError, URLError -> OSError.)
        print(f"[error] {e}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
