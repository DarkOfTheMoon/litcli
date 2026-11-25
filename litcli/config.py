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

from .backends import import_errors as _IMPORT_ERRORS
from .backends import load_module
from .backends._entitled import EntitledBackend
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


def _try_import(reg: dict[str, type[Backend]], name: str, attr: str) -> None:
    """Register ``attr`` of ``backends.<name>``, or record why it cannot be imported.

    Every optional backend is tried by itself: one that is not ready (a missing dependency, a
    syntax error, a bad constant) must cost only its own backend, not the whole registry. The
    import and its cached reason live in :func:`backends.load_module`, shared with
    ``publishers`` so both subsystems see one failure policy.
    """
    mod = load_module(name)
    if mod is None:
        return
    try:
        reg[name] = getattr(mod, attr)
    except Exception as e:
        _IMPORT_ERRORS[name] = f"{type(e).__name__}: {e}"


#: The optional modules, by registered name and class (``name == module`` for all of them).
_OPTIONAL_BACKENDS = (
    ("arxiv", "ArxivBackend"), ("crossref", "CrossrefBackend"), ("openalex", "OpenAlexBackend"),
    ("openlibrary", "OpenLibraryBackend"), ("uspto", "UsptoBackend"), ("cnki", "CnkiBackend"),
    ("douban", "DoubanBackend"), ("scihub", "ScihubBackend"), ("zlibrary", "ZlibraryBackend"),
    ("doaj", "DoajBackend"), ("europepmc", "EuropePmcBackend"), ("doab", "DoabBackend"),
    ("zenodo", "ZenodoBackend"), ("semanticscholar", "SemanticScholarBackend"),
    ("opencitations", "OpenCitationsBackend"),
)


@cache
def _registry() -> dict[str, type[Backend]]:
    """Backend type registry (type string -> class). Cached per-process: the try-imports are
    static, while every backend instantiation and the ``get_backend`` fallback path consult the
    registry, so caching avoids redoing the imports each time.
    """
    from .backends import zotero_local

    reg: dict[str, type[Backend]] = {"zotero_local": zotero_local.ZoteroLocalBackend}
    for name, attr in _OPTIONAL_BACKENDS:
        _try_import(reg, name, attr)
    # Every entitled/guarded publisher: its name, module and class come from the one table, so
    # a registered reader cannot be missing from the chain (and vice versa).
    try:
        from .publishers import _PUBLISHER_SPECS as specs
    except Exception as e:  # a broken table must cost only the publisher backends
        specs = ()
        _IMPORT_ERRORS["publishers"] = f"{type(e).__name__}: {e}"
    for spec in specs:
        _try_import(reg, spec.name, spec.backend)
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


def section_disabled(name: str, cfg: dict) -> bool:
    """True when ``[backends.<name>]`` is switched off (``enabled = false``).

    One predicate for the whole codebase: every caller that walks the backends table has to
    tell "the user switched it off" from "it is not configured" — and must not build the
    section either way.
    """
    section = (cfg.get("backends") or {}).get(name)
    return isinstance(section, dict) and section.get("enabled") is False


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
        if section_disabled(name, cfg):
            continue
        try:
            out[name] = _make_backend(name, bcfg)
        except Exception as e:
            if strict:
                raise
            # The registry already knows the import-time cause when there is one: "unknown
            # backend type" alone would hide it behind a name the config plainly mentions.
            failed_backends[name] = _IMPORT_ERRORS.get(name) or f"{type(e).__name__}: {e}"
    _attach_browser_session(out, cfg)
    return out


def _attach_browser_session(backends: dict, cfg: dict) -> None:
    """Hand the configured browser session to every entitled reader that wants one.

    A backend built with its own ``session`` keeps it. Without ``[browser].enabled`` this is a
    no-op, so the cookie path stays the default and no browser ever starts unasked.
    """
    if not any(isinstance(b, EntitledBackend) for b in backends.values()):
        return
    from .browser import session_from_config

    session = session_from_config(cfg)
    if session is None:
        return
    for backend in backends.values():
        if isinstance(backend, EntitledBackend) and backend.session is None:
            backend.session = session


def get_backend(name: str = "zotero", config: dict | None = None) -> Backend:
    cfg = config if config is not None else load_config()
    backends = discover_backends(cfg)
    if name in backends:
        return backends[name]
    # It is configured but its section could not be built (a bad cookie_file, say): say so
    # instead of silently handing back a default instance that ignores the section.
    if name in failed_backends:
        raise RuntimeError(f"backend '{name}' failed to build: {failed_backends[name]}")
    # A section switched off must not come back through the registry: ``fetch`` rebuilds its
    # corpora and publisher steps here, so ``enabled = false`` has to be honoured too.
    if section_disabled(name, cfg):
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
