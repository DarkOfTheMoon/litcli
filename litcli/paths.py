"""Per-user locations and file-name rules that hold on every supported OS.

Two platform differences live here rather than at each call site:

- Windows keeps per-user configuration under ``%APPDATA%``, not ``~/.config``;
- characters that are legal in a DOI (``:``, ``<``, ``>``) are illegal in a Windows file
  name, so a key cannot be used verbatim as a save name.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

APP_DIR = "litcli"

#: Characters Windows rejects in a path component (POSIX only rejects ``/`` and NUL).
#: Old-style DOIs really do contain three of them, e.g.
#: ``10.1002/(SICI)1097-0258(19970815)16:15<1705::AID-SIM599>3.0.CO;2-H``.
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

#: Device names Windows reserves even with an extension: ``CON.pdf`` still opens the device.
_RESERVED = ({"con", "prn", "aux", "nul"}
             | {f"com{i}" for i in range(1, 10)}
             | {f"lpt{i}" for i in range(1, 10)})

#: Cap the stem well under the 255-character component limit (leaves room for an extension
#: and for the directory the file is later moved into).
_MAX_STEM = 120

#: Resolved once: patching ``os.name`` at test time would also make :mod:`pathlib` hand out
#: ``WindowsPath`` objects, which cannot be built on POSIX.
_IS_WINDOWS = os.name == "nt"


def config_dir() -> Path:
    """Per-user configuration directory.

    ``%APPDATA%\\litcli`` on Windows, ``$XDG_CONFIG_HOME/litcli`` (default
    ``~/.config/litcli``) elsewhere. Resolved per call so a test or a wrapper can set the
    variable after import.
    """
    if _IS_WINDOWS:
        base = os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(base) / APP_DIR


def safe_filename(stem: str, ext: str) -> str:
    """Turn an item key into a file name every supported OS accepts.

    Illegal characters become ``_``; trailing dots and spaces (which Windows silently
    strips, so ``a.`` and ``a`` would collide) are trimmed, an over-long stem is truncated,
    and a reserved device name gets a trailing ``_``.
    """
    name = re.sub(r"_+", "_", _ILLEGAL.sub("_", stem)).strip("_. ")
    name = name[:_MAX_STEM].rstrip(". _")
    if not name:
        name = "download"
    head, dot, rest = name.partition(".")
    if head.lower() in _RESERVED:
        name = f"{head}_{dot}{rest}"
    return f"{name}.{ext.lstrip('.')}"


def target_path(out: str | None, stem: str, ext: str) -> str:
    """Resolve ``--out`` plus a default file name.

    ``--out`` may be a full path, or a directory (existing, or written with a trailing
    separator) to save the default name inside; with no ``--out`` the name alone is
    returned, i.e. the current directory.
    """
    name = safe_filename(stem, ext)
    if not out:
        return name
    if out.endswith(("/", "\\")) or Path(out).is_dir():
        return str(Path(out) / name)
    return out
