"""litcli entry point: build the argparse surface, apply the config, dispatch the command.

The commands live in ``litcli.commands``, one module per intent family; this module only
parses, sets up the process (config, proxy) and maps ``args`` onto them. Write commands
first check the backend's capability; read-only backends raise ``NotSupportedError``,
reported here as ``[skipped]``.
"""
from __future__ import annotations

import contextlib
import os
import sys

from . import cache
from .commands import browser as browser_cmd
from .commands import data as data_cmd
from .commands import diagnostics, export, fetch, graph, search
from .commands import survey as survey_cmd
from .commands.parser import build_parser
from .config import load_config
from .debug import enabled as debug_enabled
from .debug import log, mask, set_enabled, set_verbose
from .errors import LitcliError
from .transport import set_proxy

#: Top-level commands that take only ``(cfg, args)``; the graph edges and the two subcommand
#: families need their subcommand name as well and are dispatched separately.
_TOP_LEVEL_COMMANDS = {
    "search": search.cmd_search,
    "export": export.cmd_export,
    "completions": diagnostics.cmd_completions,
    "backends": diagnostics.cmd_backends,
}

_SURVEY_COMMANDS = {
    "new": survey_cmd.cmd_survey_new,
    "run": survey_cmd.cmd_survey_run,
}

_BROWSER_COMMANDS = {
    "login": browser_cmd.cmd_browser_login,
    "status": browser_cmd.cmd_browser_status,
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
    # Any proxy already in the environment wins, HTTP_PROXY included (httpx reads both schemes).
    if any(os.environ.get(v) for v in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy")):
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


#: Flags accepted in front of *or* behind the subcommand; they are stripped from argv before
#: argparse runs, and only when they are not the value of a preceding ``--flag``.
_GLOBAL_FLAGS = ("--debug", "-v", "--verbose", "--no-cache")


def _value_options(parser) -> set[str]:
    """Every option string that takes a value, across the whole parser tree.

    Used to tell ``search --title --verbose`` (a value) from ``search x --verbose`` (a flag).
    """
    found: set[str] = set()
    stack = [parser]
    while stack:
        current = stack.pop()
        actions = getattr(current, "_actions", None)
        for action in (actions if isinstance(actions, list) else []):
            if action.option_strings and action.nargs != 0:
                found.update(action.option_strings)
            choices = getattr(action, "choices", None)
            if isinstance(choices, dict):
                stack.extend(choices.values())
    return found


def _split_global_flags(argv: list[str], value_options: set[str]) -> tuple[list[str], bool, bool]:
    """Pull the global flags out of ``argv`` and report ``(debug, verbose)``.

    A flag is only taken when it cannot be the value of the preceding token (the token before it
    ends in ``=`` or is an option that takes a value), so ``search --title --debug`` keeps
    ``--debug`` as the title while ``search x --debug`` turns tracing on.
    """
    kept: list[str] = []
    debug = verbose = False
    for arg in argv:
        if arg in _GLOBAL_FLAGS and not (kept and (kept[-1].endswith("=") or kept[-1] in value_options)):
            debug = debug or arg == "--debug"
            verbose = verbose or arg in ("-v", "--verbose")
            continue
        kept.append(arg)
    return kept, debug, verbose


def main(argv=None) -> int:
    _utf8_redirected_streams()
    # ``--debug`` is accepted anywhere (before or after the subcommand): strip it first, then let
    # argparse see the rest. It is registered on the root parser so it shows up in --help.
    raw = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    quiet, debug_flag, verbose_flag = _split_global_flags(raw, _value_options(parser))
    args = parser.parse_args(quiet)
    if debug_flag or getattr(args, "debug", False):
        set_enabled()
        # The argv line carries any URL the user pasted, including its ``mailto=`` address.
        log(f"litcli {mask(' '.join(quiet))}")
    if verbose_flag or getattr(args, "verbose", False):
        set_verbose()
    if "--no-cache" in raw or getattr(args, "no_cache", False):
        cache.disable()
    try:
        cfg = load_config()
        _apply_proxy(cfg)
        if args.cmd in _TOP_LEVEL_COMMANDS:
            return _TOP_LEVEL_COMMANDS[args.cmd](cfg, args)
        if args.cmd in graph.EDGE_METHODS:
            return graph.cmd_edge(cfg, args, graph.EDGE_METHODS[args.cmd])
        if args.cmd == "survey":
            return _SURVEY_COMMANDS[args.survey_cmd](cfg, args)
        if args.cmd == "browser":
            return _BROWSER_COMMANDS[args.browser_cmd](cfg, args)
        if args.cmd == "data":
            return _DATA_COMMANDS[args.data_cmd](cfg, args)
        if args.cmd == "fetch":
            return fetch.cmd_fetch(cfg, args)
        print(f"[error] unknown command {args.cmd}", file=sys.stderr)
        return 2
    except LitcliError as e:
        # Every expected failure carries a kind and an exit code; the kind only drives the
        # label here, the policy that led to the raise lives in the caller. Diagnostics go to
        # stderr so ``--format json`` on stdout stays parseable.
        print(f"{'[skipped]' if e.kind == 'unsupported' else '[error]'} {e}", file=sys.stderr)
        if e.hint:
            print(f"[hint] {e.hint}", file=sys.stderr)
        return e.exit_code
    except BrokenPipeError:
        # ``litcli ... | head`` closes the pipe first. The work is done, so this is not a
        # failure: exit 0 quietly, and point stdout at devnull so the interpreter's final
        # flush does not print an "Exception ignored" traceback.
        with contextlib.suppress(OSError, ValueError, AttributeError):
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
        return 0
    except KeyError as e:
        print(f"[error] {e}", file=sys.stderr)
        return 2
    except (RuntimeError, ValueError, SyntaxError, OSError) as e:
        # Backend business errors (HTTP 401/404/timeout) and data-shape failures (non-JSON response,
        # malformed XML, config TOML syntax) are surfaced gracefully, never as a raw stack trace.
        # (json.JSONDecodeError -> ValueError, lxml XMLSyntaxError -> SyntaxError,
        #  TOMLDecodeError -> ValueError, URLError -> OSError.)
        print(f"[error] {e}", file=sys.stderr)
        return 2
    except Exception as e:
        # Anything unexpected is still a failure, not the "not found" exit 1 a traceback would
        # leave behind (scripts read 1 as fetch's no-fulltext answer).
        if debug_enabled():
            import traceback
            traceback.print_exc()
        print(f"[error] unexpected: {type(e).__name__}: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
