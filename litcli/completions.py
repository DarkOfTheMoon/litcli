"""Shell completion: ``litcli completions <bash|zsh|fish>`` prints a self-contained script.

Zero-third-party-dependency: the completion spec is introspected once from the argparse parser
and rendered, per shell, as a static table + a small engine. At tab time no ``litcli`` subprocess
is spawned — the sourced script completes locally from its embedded spec. Re-source (or re-run
``eval "$(litcli completions <shell>)"``) whenever the CLI surface changes.

Scope: subcommands, per-command flags, option choice values (``--format``/``--sort``/``--backend``/
``--type``), and local filenames for file-valued flags/positionals. Key/query positionals are not
completed (they need live data).
"""
from __future__ import annotations

import argparse

# argparse does not encode these as ``choices``; supply the known values for completion.
BACKENDS = ["zotero", "arxiv", "crossref", "openalex", "openlibrary", "uspto", "cnki", "douban", "scihub"]
TYPES = ["article", "book", "chapter", "thesis", "report", "preprint", "paper",
         "patent", "document", "dataset", "software", "conference"]
SHELL_CHOICES = ["bash", "zsh", "fish"]

# Flags that take a local file path (value completion via the filesystem).
FILE_FLAGS = {"--pdf", "--csl", "--out"}
# (command path, relative positional index) -> name, completed as a file path. The relative
# index counts non-command words that follow the resolved command path (``data attach`` has
# ``key`` at 1 and ``src`` at 2).
FILE_POSITIONALS = {("data", "attach"): {2: "src"}}


def build_spec(parser: argparse.ArgumentParser) -> dict:
    """Introspect an argparse parser into a flat completion spec.

    Returns ``{"top": [...], "subs": {path: [children]}, "opts": {path: {flag: choices-or-None}},
    "files": [file flags], "file_positionals": {(path, rel_index): name}}`` where ``path`` is a
    tuple of command names (``()`` for the root).
    """
    spec: dict = {"top": [], "subs": {}, "opts": {}, "files": sorted(FILE_FLAGS),
                  "file_positionals": FILE_POSITIONALS}

    def visit(parser_: argparse.ArgumentParser, path: tuple) -> None:
        opts: dict = {}
        for act in parser_._actions:
            if isinstance(act, argparse._SubParsersAction):
                children = [name for name in act.choices.keys()]
                if path:
                    spec["subs"][path] = children
                else:
                    spec["top"] = children
                for name, sub in act.choices.items():
                    visit(sub, path + (name,))
            elif act.option_strings:
                for flag in act.option_strings:
                    if flag in ("-h", "--help"):
                        continue
                    choices = _flag_choices(act)
                    opts[flag] = choices
        # Root options (e.g. ``--version``) are keyed at ``()`` so they complete too.
        spec["opts"][path] = opts

    visit(parser, ())

    # Overlays: completions takes the shell name as a value positional; backends/types incur
    # completion from the known value lists that argparse does not expose as choices.
    spec["subs"][("completions",)] = SHELL_CHOICES
    spec["opts"][("completions",)] = {}
    for opts in spec["opts"].values():
        if "--backend" in opts:
            opts["--backend"] = BACKENDS
        if "--type" in opts:
            opts["--type"] = TYPES
    return spec


def _flag_choices(act: argparse.Action) -> list | None:
    """Return the choices of an option action as a list, or None."""
    choices = getattr(act, "choices", None)
    if isinstance(choices, (list, tuple)):
        return list(choices)
    return None


def _path_key(path: tuple) -> str:
    """Space-joined command path for use as a shell ``case`` tag (``''`` for the root)."""
    return " ".join(path)


def _pos(path: tuple, rel: int) -> str:
    """Name of the file-positional at a command path/relative index, or ''. """
    return str(FILE_POSITIONALS.get(path, {}).get(rel, ""))


def render(shell: str, spec: dict) -> str:
    """Render the completion script for ``shell`` from ``spec``."""
    renderers = {"bash": _render_bash, "zsh": _render_zsh, "fish": _render_fish}
    return renderers[shell](spec)


def _sub_arms(spec: dict) -> list[str]:
    """``case`` arms mapping a parent path to its child commands (root -> top commands)."""
    arms = [f'    "") echo "{_join(spec["top"])}" ;;']
    for path, children in spec["subs"].items():
        if not children:
            continue
        arms.append(f'    "{_path_key(path)}") echo "{_join(children)}" ;;')
    return arms


def _flag_arms(spec: dict) -> list[str]:
    """``case`` arms mapping a command path to its option flags (paths with flags only)."""
    arms = []
    for path, flags in spec["opts"].items():
        if not flags:
            continue
        arms.append(f'    "{_path_key(path)}") echo "{_join(sorted(flags))}" ;;')
    return arms


def _choice_arms(spec: dict) -> list[str]:
    """``case`` arms mapping ``path|flag`` to its choice values (flags with choices only)."""
    arms = []
    for path, flags in spec["opts"].items():
        for flag, choices in sorted(flags.items()):
            if not choices:
                continue
            arms.append(f'    "{_path_key(path)}|{flag}") echo "{_join(choices)}" ;;')
    return arms


def _fileflag_arms(spec: dict) -> list[str]:
    """``case`` arms marking flags that take a local file path."""
    arms = []
    for path, flags in spec["opts"].items():
        for flag in sorted(flags):
            if flag in spec["files"]:
                arms.append(f'    "{_path_key(path)}|{flag}") echo 1 ;;')
    return arms


def _filepos_arms(spec: dict) -> list[str]:
    """``case`` arms marking a command path's relative positional that is a file path."""
    arms = []
    for (path, table) in spec["file_positionals"].items():
        for rel in sorted(table):
            arms.append(f'    "{_path_key(path)}|{rel}") echo 1 ;;')
    return arms


def _join(tokens) -> str:
    return " ".join(token for token in tokens if token)


_HEADER = """#!/usr/bin/env bash
# litcli shell completion (generated by `litcli completions bash`; re-run to refresh).
# Source it: eval "$(litcli completions bash)"
_litcli_sub() {
    case "$1" in
{_subs}
    esac
}
_litcli_flags() {
    case "$1" in
{_flags}
    esac
}
_litcli_choices() {
    case "$1|$2" in
{_choices}
    esac
}
_litcli_file() {
    case "$1|$2" in
{_fileflags}
    esac
}
_litcli_filepos() {
    case "$1|$2" in
{_filepos}
    esac
}
_litcli_is_cmd() {
    _litcli_sub "$1" | tr ' ' '\\n' | grep -qx -- "$2"
}
_litcli_complete() {
    local cur="${COMP_WORDS[COMP_CWORD]}"
    local prev="${COMP_WORDS[COMP_CWORD-1]}"
    local i w path="" cand ncmd=0
    for ((i=1; i<COMP_CWORD; i++)); do
        w="${COMP_WORDS[i]}"
        if _litcli_is_cmd "$path" "$w"; then
            path="${path:+$path }$w"
            ncmd=$((ncmd+1))
        else
            break
        fi
    done
    local rel=$((COMP_CWORD - ncmd))
    COMPREPLY=()
    if [[ "$cur" == -* ]]; then
        for cand in $(_litcli_flags "$path"); do
            [[ "$cand" == "$cur"* ]] && COMPREPLY+=("$cand")
        done
    elif [[ "$prev" == -* ]]; then
        local ch
        ch=$(_litcli_choices "$path" "$prev")
        if [[ -n "$ch" ]]; then
            for cand in $ch; do [[ "$cand" == "$cur"* ]] && COMPREPLY+=("$cand"); done
        elif [[ "$(_litcli_file "$path" "$prev")" == "1" ]]; then
            COMPREPLY=($(compgen -f -- "$cur"))
        fi
    elif [[ "$COMP_CWORD" -eq $((ncmd+1)) ]]; then
        local subs
        subs=$(_litcli_sub "$path")
        if [[ -n "$subs" ]]; then
            for cand in $subs; do [[ "$cand" == "$cur"* ]] && COMPREPLY+=("$cand"); done
        fi
    elif [[ "$(_litcli_filepos "$path" "$rel")" == "1" ]]; then
        COMPREPLY=($(compgen -f -- "$cur"))
    fi
    return 0
}
complete -F _litcli_complete litcli
"""


def _fill(template: str, **parts: str) -> str:
    """Fill ``{name}`` placeholders via replace() so literal ``{``/``}`` in the script body
    (bash/zsh ``${VAR}`` expansions) are left untouched — ``str.format`` would misread them."""
    for name, value in parts.items():
        template = template.replace("{" + name + "}", value)
    return template


def _render_bash(spec: dict) -> str:
    return _fill(
        _HEADER,
        _subs="\n".join(_sub_arms(spec)),
        _flags="\n".join(_flag_arms(spec)),
        _choices="\n".join(_choice_arms(spec)),
        _fileflags="\n".join(_fileflag_arms(spec)),
        _filepos="\n".join(_filepos_arms(spec)),
    )


_ZSH_HEADER = """#compdef litcli
# litcli zsh completion (generated by `litcli completions zsh`; re-run to refresh).
# Source it: eval "$(litcli completions zsh)"
_litcli_sub() {
    case "$1" in
{_subs}
    esac
}
_litcli_flags() {
    case "$1" in
{_flags}
    esac
}
_litcli_choices() {
    case "$1|$2" in
{_choices}
    esac
}
_litcli_file() {
    case "$1|$2" in
{_fileflags}
    esac
}
_litcli_filepos() {
    case "$1|$2" in
{_filepos}
    esac
}
_litcli_is_cmd() {
    _litcli_sub "$1" | tr ' ' '\\n' | grep -qx -- "$2"
}
_litcli() {
    local cur="${words[CURRENT]}" prev="${words[CURRENT-1]}"
    local path="" w i ncmd=0
    local rel
    if [[ "$_litcli_cur" != "$words[CURRENT]" ]]; then
        for ((i=1; i<CURRENT; i++)); do
            w="${words[i]}"
            if _litcli_is_cmd "$path" "$w"; then
                path="${path:+$path }$w"; ncmd=$((ncmd+1))
            else
                break
            fi
        done
        _litcli_path="$path"; _litcli_ncmd=$ncmd
    fi
    path="$_litcli_path"; ncmd=$_litcli_ncmd
    rel=$((CURRENT - ncmd))
    if [[ "$cur" == -* ]]; then
        compadd -- ${=_litcli_flags "$path"}
    elif [[ "$prev" == -* ]]; then
        local ch=$(_litcli_choices "$path" "$prev")
        if [[ -n "$ch" ]]; then
            compadd -- ${=ch}
        elif [[ "$(_litcli_file "$path" "$prev")" == "1" ]]; then
            _files
        fi
    elif [[ "$CURRENT" -eq $((ncmd+1)) ]]; then
        local subs=$(_litcli_sub "$path")
        [[ -n "$subs" ]] && compadd -- ${=subs}
    elif [[ "$(_litcli_filepos "$path" "$rel")" == "1" ]]; then
        _files
    fi
}
compdef _litcli litcli
"""


def _render_zsh(spec: dict) -> str:
    return _fill(
        _ZSH_HEADER,
        _subs="\n".join(_sub_arms(spec)),
        _flags="\n".join(_flag_arms(spec)),
        _choices="\n".join(_choice_arms(spec)),
        _fileflags="\n".join(_fileflag_arms(spec)),
        _filepos="\n".join(_filepos_arms(spec)),
    )


def _fish_lines(spec: dict) -> list[str]:
    """fish uses declarative ``complete`` rules (no engine); emitted per command/flag."""
    lines = []
    lines.append(f"complete -c litcli -n '__fish_use_subcommand' -a '{_join(spec['top'])}'")
    for path, children in spec["subs"].items():
        if not children:
            continue
        seen = " ".join(path)
        lines.append(f"complete -c litcli -n '__fish_seen_subcommand_from {seen}' -a '{_join(children)}'")
    for path, flags in spec["opts"].items():
        seen = " ".join(path)
        # Root options (``--version``) only apply before a subcommand is chosen.
        cond = "__fish_use_subcommand" if not seen else f"__fish_seen_subcommand_from {seen}"
        for flag in sorted(flags):
            # argparse token style is ``--flag``; fish wants the short ``-l name`` form.
            name = flag[2:]
            choices = flags[flag]
            if choices:
                lines.append(f"complete -c litcli -n '{cond}' -l {name} -a '{_join(choices)}'")
            elif flag in spec["files"]:
                lines.append(f"complete -c litcli -n '{cond}' -l {name} -r -F")
            elif _boolean_flag(flag):
                lines.append(f"complete -c litcli -n '{cond}' -l {name}")
    for (path, table) in spec["file_positionals"].items():
        seen = " ".join(path)
        lines.append(f"complete -c litcli -n '__fish_seen_subcommand_from {seen}' -r -F")
    return lines


def _boolean_flag(flag: str) -> bool:
    """store_true/-type flags take no value; fish registers them without ``-r``."""
    return flag in {"--all", "--no-cite", "--no-recursive", "--force", "--download", "--version"}


def _render_fish(spec: dict) -> str:
    body = "\n".join(_fish_lines(spec))
    return ("# litcli fish completion (generated by `litcli completions fish`; re-run to refresh).\n"
            f"{body}\n")
