"""Per-user locations and file-name rules that hold on every supported OS.

Two platform differences live here rather than at each call site:

- Windows keeps per-user configuration under ``%APPDATA%``, not ``~/.config``;
- characters that are legal in a DOI (``:``, ``<``, ``>``) are illegal in a Windows file
  name, so a key cannot be used verbatim as a save name.
"""
from __future__ import annotations

import os
import re
import sys
import unicodedata
from pathlib import Path

APP_DIR = "litcli"

#: Characters Windows rejects in a path component (POSIX only rejects ``/`` and NUL).
#: Old-style DOIs really do contain three of them, e.g.
#: ``10.1002/(SICI)1097-0258(19970815)16:15<1705::AID-SIM599>3.0.CO;2-H``.
#: DEL is included with the C0 range; Unicode format characters (``Cf``: zero-width space,
#: soft hyphen, the LRM/RLM marks copy-pasted from PDFs) are dropped separately.
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')

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


def is_frozen() -> bool:
    """True in a PyInstaller build (the binary installers), False from source or a wheel.

    Browser backends load ``playwright`` at runtime; in a frozen build it ships alongside, but
    the browser itself is downloaded separately, so the hint differs.
    """
    return bool(getattr(sys, "frozen", False))


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
    stem = "".join(ch for ch in stem if unicodedata.category(ch) != "Cf")
    name = re.sub(r"_+", "_", _ILLEGAL.sub("_", stem)).strip("_. ")
    name = name[:_MAX_STEM].rstrip(". _")
    if not name:
        name = "download"
    head, dot, rest = name.partition(".")
    if head.lower() in _RESERVED:
        name = f"{head}_{dot}{rest}"
    return f"{name}.{ext.lstrip('.')}"


def browser_cache_dir() -> Path:
    """Where downloaded browsers live (Playwright's own per-user cache).

    A frozen Playwright forces ``PLAYWRIGHT_BROWSERS_PATH=0`` on the driver it starts, i.e.
    browsers *inside* the bundle — a tree the installer wipes on upgrade and uninstall. Point
    the download and the launch at the shared per-user cache instead, so a browser survives
    both.
    """
    if _IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "ms-playwright"
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "ms-playwright"


def cache_dir() -> Path:
    r"""Per-user HTTP response cache (metadata reads only; files are not cached).

    ``%LOCALAPPDATA%\litcli\cache`` on Windows, ``$XDG_CACHE_HOME/litcli/cache``
    (default ``~/.cache/litcli/cache``) elsewhere.
    """
    if _IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
    else:
        base = os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")
    return Path(base) / APP_DIR / "cache"


def _data_dir() -> Path:
    """Per-user data directory (``%LOCALAPPDATA%`` / ``$XDG_DATA_HOME``, default ``~/.local/share``)."""
    if _IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
    else:
        base = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    return Path(base) / APP_DIR


def project_root() -> Path:
    """Where survey projects live, one directory per project.

    Unlike the cache these are the user's own work, so they sit under the data dir and
    survive a cache clear.
    """
    return _data_dir() / "projects"


def browser_private_xdg() -> Path:
    """A private ``XDG_CONFIG_HOME`` for a browser started on the user's own profile.

    Chrome refuses a debugging port while ``--user-data-dir`` resolves to its platform
    default, and it works that default out from ``XDG_CONFIG_HOME``. Handing the child a
    private one makes the named directory non-default again; the cookie store stays the real
    one, since the cookies live in the directory, not under the config home.
    """
    return _data_dir() / "browser-xdg"


def browser_profile_dir() -> Path:
    """The one Chrome profile every entitled publisher shares.

    Sharing it is the point: a single institutional sign-in covers all the sites, and the
    clearance cookies are renewed by the browser itself instead of being copied by hand.
    """
    return _data_dir() / "browser-profile"


def user_browser_profile_dir() -> Path:
    """Where the user's own Chrome keeps its profile: the sign-ins are already in there.

    Nothing is launched into it by default — an existing browser on it is attached to
    instead, and a closed one is started from it.
    """
    if _IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
        return Path(base) / "Google" / "Chrome" / "User Data"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
    return Path.home() / ".config" / "google-chrome"


def resolve_browser_profile(value: str | None) -> Path:
    """``[browser].profile`` as a directory: empty/``user`` is theirs, ``dedicated`` is ours."""
    if not value or value == "user":
        return user_browser_profile_dir()
    if value == "dedicated":
        return browser_profile_dir()
    return Path(value).expanduser()


def browser_session_file() -> Path:
    """Discovery file of a running browser session (endpoint + pid); absent when none runs."""
    return _data_dir() / "browser-session.json"


def apply_browser_cache() -> Path:
    """Make Playwright resolve browsers from :func:`browser_cache_dir` and return it.

    ``setdefault`` on purpose: an explicit ``PLAYWRIGHT_BROWSERS_PATH`` (``0`` included) wins.
    """
    cache = browser_cache_dir()
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(cache))
    return cache


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
