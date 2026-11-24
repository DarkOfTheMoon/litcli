"""Config & backend discovery: read ~/.config/litcli/config.toml and instantiate backends by type.

Parsed with :mod:`tomllib` (built into Python 3.11+).
The backend registry is try-import, so Zotero alone can work even when other backend files
are not ready.
"""
from __future__ import annotations

import copy
import os
import tomllib
from functools import cache
from pathlib import Path

from .backends.base import Backend
from .errors import BadInput
from .paths import config_dir

# Built-in default backends used when no config file exists: local Zotero (read/write) plus the
# three keyless metadata sources, which work out of the box.
_INBUILT_DEFAULT = {"backends": {
    "zotero": {"type": "zotero_local", "base": "http://localhost:23119/api"},
    "arxiv": {"type": "arxiv"},
    "crossref": {"type": "crossref"},
    "openalex": {"type": "openalex"},
}}

CONFIG_ENV = "LITCLI_CONFIG"


def default_config_path() -> Path:
    """The per-user config file. Resolved per call, so an ``XDG_CONFIG_HOME`` set after import
    (a wrapper, a test) still points the default at the right place."""
    return config_dir() / "config.toml"


def load_config(path: str | Path | None = None) -> dict:
    """Load config; precedence: explicit path > env var LITCLI_CONFIG > the per-user default
    (``%APPDATA%\\litcli`` on Windows, ``~/.config/litcli`` elsewhere) > built-in default.

    An explicitly named file (argument or ``LITCLI_CONFIG``) must exist: ``write_backend_key``
    writes to that same path, so silently falling back to the defaults here would have a
    mistyped variable create the file it never read.
    """
    explicit = Path(path) if path is not None else (
        Path(os.environ[CONFIG_ENV]) if CONFIG_ENV in os.environ else None)
    if explicit is not None:
        if not explicit.exists():
            source = " (from LITCLI_CONFIG)" if path is None else ""
            raise BadInput(f"config file not found: {explicit}{source}")
        with open(explicit, "rb") as f:
            return tomllib.load(f)
    default = default_config_path()
    if default.exists():
        with open(default, "rb") as f:
            return tomllib.load(f)
    # Deep copy: callers patch the dict (api_key, proxy), and the module constant must not leak
    # those edits into the next load.
    return copy.deepcopy(_INBUILT_DEFAULT)


@cache
def _registry() -> dict[str, type[Backend]]:
    """Backend type registry (type string -> class). Cached per-process: the try-import
    blocks are static, while every backend instantiation and the ``get_backend`` fallback
    path consult the registry, so caching avoids redoing the imports each time.
    """
    from .backends import zotero_local

    # Every optional module is tried by itself: one that is not ready (missing dependency, a
    # syntax error, a bad constant) must cost only its own backend, not the whole registry.
    reg: dict[str, type[Backend]] = {"zotero_local": zotero_local.ZoteroLocalBackend}
    try:
        from .backends import arxiv

        reg["arxiv"] = arxiv.ArxivBackend
    except Exception:
        pass  # When arXiv isn't ready, it doesn't affect the Zotero definition
    try:
        from .backends import crossref

        reg["crossref"] = crossref.CrossrefBackend
    except Exception:
        pass
    try:
        from .backends import openalex

        reg["openalex"] = openalex.OpenAlexBackend
    except Exception:
        pass
    try:
        from .backends import openlibrary

        reg["openlibrary"] = openlibrary.OpenLibraryBackend
    except Exception:
        pass
    try:
        from .backends import uspto

        reg["uspto"] = uspto.UsptoBackend
    except Exception:
        pass
    try:
        from .backends import cnki

        reg["cnki"] = cnki.CnkiBackend
    except Exception:
        pass
    try:
        from .backends import douban

        reg["douban"] = douban.DoubanBackend
    except Exception:
        pass
    try:
        from .backends import scihub

        reg["scihub"] = scihub.ScihubBackend
    except Exception:
        pass
    # Every entitled/guarded publisher: its name, module and class come from the one table, so
    # a registered reader cannot be missing from the chain (and vice versa).
    from importlib import import_module

    try:  # the table imports every publisher module: one missing must not sink the registry
        from .backends.publishers import _PUBLISHER_SPECS
    except Exception:
        _PUBLISHER_SPECS = ()

    # "Not ready" is anything the import raises — a missing module (``ImportError``), but also
    # a syntax error, a bad constant or a renamed class, none of which is an ``ImportError``.
    for spec in _PUBLISHER_SPECS:
        try:
            module = import_module(f".backends.{spec.name}", __package__)
            reg[spec.name] = getattr(module, spec.backend)
        except Exception:
            pass
    try:
        from .backends import zlibrary

        reg["zlibrary"] = zlibrary.ZlibraryBackend
    except Exception:
        pass
    try:
        from .backends import doaj

        reg["doaj"] = doaj.DoajBackend
    except Exception:
        pass
    try:
        from .backends import europepmc

        reg["europepmc"] = europepmc.EuropePmcBackend
    except Exception:
        pass
    try:
        from .backends import doab

        reg["doab"] = doab.DoabBackend
    except Exception:
        pass
    try:
        from .backends import zenodo

        reg["zenodo"] = zenodo.ZenodoBackend
    except Exception:
        pass
    return reg


def _make_backend(name: str, cfg: dict) -> Backend:
    reg = _registry()
    backend_type = cfg.get("type", "zotero_local")
    if backend_type not in reg:
        raise KeyError(f"unknown backend type '{backend_type}' (available: {sorted(reg)})")
    # ``name`` and ``enabled`` are config-only keys; the section name is the backend's identity
    # and a constructor knows neither, so they must not reach ``**kwargs``.
    kwargs = {k: v for k, v in cfg.items() if k not in ("type", "name", "enabled")}
    return reg[backend_type](name=name, **kwargs)


#: Backends that could not be built, with the reason (a missing optional dependency, say).
#: Filled by ``discover_backends`` so ``backends list/status`` can name them.
failed_backends: dict[str, str] = {}


def discover_backends(config: dict | None = None, strict: bool = False) -> dict[str, Backend]:
    """Instantiate every backend from config, skipping sections marked ``enabled = false``.

    One broken backend (missing optional dependency, bad config value, unknown type) must not
    take the others down, so construction failures are collected in :data:`failed_backends`
    instead of propagating — unless ``strict``. ``get_backend`` still raises when a named
    backend is the one that failed.
    """
    cfg = config if config is not None else load_config()
    out: dict[str, Backend] = {}
    for name, bcfg in (cfg.get("backends") or {}).items():
        if not isinstance(bcfg, dict):
            failed_backends[name] = f"TypeError: section is a {type(bcfg).__name__}, not a table"
            if strict:
                raise TypeError(f"[backends.{name}] is not a table")
            continue
        if bcfg.get("enabled") is False:
            continue
        try:
            out[name] = _make_backend(name, bcfg)
        except Exception as e:
            if strict:
                raise
            failed_backends[name] = f"{type(e).__name__}: {e}"
    return out


def get_backend(name: str = "zotero", config: dict | None = None) -> Backend:
    backends = discover_backends(config)
    if name in backends:
        return backends[name]
    # It is configured but its section could not be built (a bad cookie_file, say): say so
    # instead of silently handing back a default instance that ignores the section.
    if name in failed_backends:
        raise RuntimeError(f"backend '{name}' failed to build: {failed_backends[name]}")
    # A section switched off must not come back through the registry: ``fetch`` rebuilds its
    # corpora and publisher steps here, so ``enabled = false`` has to be honoured too.
    cfg = config if config is not None else load_config()
    section = (cfg.get("backends") or {}).get(name)
    if isinstance(section, dict) and section.get("enabled") is False:
        raise RuntimeError(f"backend '{name}' is disabled in the config (enabled = false)")
    # Allow a registered-but-unconfigured optional backend to be requested directly (e.g.
    # `search --backend douban` / `--backend cnki`) with default kwargs, rather than requiring
    # it to be enabled in config. Optional backends have type == canonical name; the built-in
    # defaults are resolved above, so this only triggers for optional ones.
    reg = _registry()
    if name in reg:
        return _make_backend(name, {"type": name})
    raise KeyError(f"backend '{name}' is not configured; configured: {sorted(backends)}")


#: Keyless open-access corpora search consults even when they are not configured.
CORPUS_BACKENDS = ("doaj", "europepmc", "doab", "zenodo")


def get_all_backends(config: dict | None = None, corpora: bool = False) -> dict[str, Backend]:
    """Enabled backends; with ``corpora`` also the keyless OA sources, configured or not."""
    out = discover_backends(config)
    if not corpora:
        return out
    for name in CORPUS_BACKENDS:
        if name in out:
            continue
        try:
            out[name] = get_backend(name, config)
        except (KeyError, RuntimeError):  # unregistered, or its section failed to build
            continue
    return out


# --- Write back to config.toml (for `backends authorize` to persist the key) ---

#: TOML basic-string escapes (a literal tab/newline in a key would otherwise break the file).
_TOML_ESCAPES = {"\\": "\\\\", '"': '\\"', "\b": "\\b", "\t": "\\t", "\n": "\\n",
                 "\f": "\\f", "\r": "\\r"}


def _toml_string(text: str) -> str:
    """Write a value as a TOML basic string, escaping quotes, backslashes and control chars."""
    parts = []
    for ch in text:
        if ch in _TOML_ESCAPES:
            parts.append(_TOML_ESCAPES[ch])
        elif ch < " " or ch == "\x7f":
            parts.append(f"\\u{ord(ch):04X}")
        else:
            parts.append(ch)
    return '"' + "".join(parts) + '"'


def _upsert_key(text: str, section: str, key: str, value: str) -> str:
    """Insert/replace a key = value line within [section]; append the section if absent. Preserve the rest."""
    lines = text.splitlines()
    marker = f"[{section}]"
    try:
        idx = next(i for i, ln in enumerate(lines) if ln.strip() == marker)
    except StopIteration:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(marker)
        lines.append(f"{key} = {value}")
        return "\n".join(lines) + "\n"
    # Find where this section ends (next [header] or end of file)
    end = idx + 1
    while end < len(lines) and not lines[end].lstrip().startswith("["):
        end += 1
    replace_at = None
    for i in range(idx + 1, end):
        if lines[i].split("=", 1)[0].strip() == key:
            replace_at = i
            break
    new_line = f"{key} = {value}"
    if replace_at is not None:
        lines[replace_at] = new_line
    else:
        lines.insert(end, new_line)
    return "\n".join(lines) + "\n"


def write_backend_key(name: str, key: str, path: str | Path | None = None) -> Path:
    """Write a backend's api_key back to the [backends.<name>] section of config.toml.

    Only the target field is modified, preserving other sections and comments in the file;
    if the file doesn't exist, write the built-in default skeleton first so no default backend
    is lost. Returns the actual file path written.
    """
    path = Path(path) if path else (
        Path(os.environ[CONFIG_ENV]) if CONFIG_ENV in os.environ else default_config_path())
    text = path.read_text(encoding="utf-8") if path.exists() else _default_toml_text()
    text = _upsert_key(text, f"backends.{name}", "api_key", _toml_string(key))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    # The file now holds a credential (api_key); keep it readable only by the owner. On Windows
    # this only clears the read-only attribute — there the containing %APPDATA% profile ACL is
    # what actually restricts access.
    os.chmod(path, 0o600)
    return path


def _default_toml_text() -> str:
    """Render the built-in default backends as config.toml text (skeleton when the file is missing)."""
    lines: list[str] = []
    for name, cfg in _INBUILT_DEFAULT["backends"].items():
        lines.append(f"[backends.{name}]")
        for k, v in cfg.items():
            if isinstance(v, bool):
                lines.append(f"{k} = {'true' if v else 'false'}")
            else:
                lines.append(f"{k} = {_toml_string(str(v))}")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"
