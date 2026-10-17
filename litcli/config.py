"""Config & backend discovery: read ~/.config/litcli/config.toml and instantiate backends by type.

Uses :mod:`tomllib` (built into Python 3.11+), zero dependencies.
The backend registry is try-import, so Zotero alone can work even when other backend files
are not ready.
"""
from __future__ import annotations

import os
import tomllib
from pathlib import Path

from .backends.base import Backend

# Key: built-in default backends when nothing is configured (local Zotero read/write + arXiv read-only, works out of the box)
_INBUILT_DEFAULT = {"backends": {
    "zotero": {"type": "zotero_local", "base": "http://localhost:23119/api"},
    "arxiv": {"type": "arxiv"},
    "crossref": {"type": "crossref"},
    "openalex": {"type": "openalex"},
}}

CONFIG_ENV = "LITCLI_CONFIG"
DEFAULT_CONFIG = Path.home() / ".config" / "litcli" / "config.toml"


def load_config(path: str | Path | None = None) -> dict:
    """Load config; precedence: explicit path > env var LITCLI_CONFIG > default ~/.config/litcli/config.toml > built-in default."""
    for candidate in (path, Path(os.environ[CONFIG_ENV]) if CONFIG_ENV in os.environ else None, DEFAULT_CONFIG):
        if candidate and Path(candidate).exists():
            with open(candidate, "rb") as f:
                return tomllib.load(f)
    return dict(_INBUILT_DEFAULT)


def _registry() -> dict[str, type[Backend]]:
    """Backend type registry (type string -> class)."""
    from .backends import zotero_local

    reg: dict[str, type[Backend]] = {"zotero_local": zotero_local.ZoteroLocalBackend}
    try:
        from .backends import arxiv

        reg["arxiv"] = arxiv.ArxivBackend
    except ImportError:
        pass  # When arXiv isn't ready, it doesn't affect the Zotero definition
    try:
        from .backends import crossref

        reg["crossref"] = crossref.CrossrefBackend
    except ImportError:
        pass
    try:
        from .backends import openalex

        reg["openalex"] = openalex.OpenAlexBackend
    except ImportError:
        pass
    try:
        from .backends import openlibrary

        reg["openlibrary"] = openlibrary.OpenLibraryBackend
    except ImportError:
        pass
    try:
        from .backends import uspto

        reg["uspto"] = uspto.UsptoBackend
    except ImportError:
        pass
    return reg


def _make_backend(name: str, cfg: dict) -> Backend:
    reg = _registry()
    backend_type = cfg.get("type", "zotero_local")
    if backend_type not in reg:
        raise KeyError(f"unknown backend type '{backend_type}' (available: {sorted(reg)})")
    kwargs = {k: v for k, v in cfg.items() if k != "type"}
    return reg[backend_type](name=name, **kwargs)


def discover_backends(config: dict | None = None) -> dict[str, Backend]:
    """Instantiate all enabled backends from config."""
    cfg = config if config is not None else load_config()
    out: dict[str, Backend] = {}
    for name, bcfg in (cfg.get("backends") or {}).items():
        out[name] = _make_backend(name, bcfg)
    return out


def get_backend(name: str = "zotero", config: dict | None = None) -> Backend:
    backends = discover_backends(config)
    if name not in backends:
        raise KeyError(f"backend '{name}' is not configured; configured: {sorted(backends)}")
    return backends[name]


def get_all_backends(config: dict | None = None) -> dict[str, Backend]:
    return discover_backends(config)


# --- Write back to config.toml (for `backends authorize` to persist the key) ---

def _toml_string(text: str) -> str:
    """Write a value as a TOML basic string (escaping quotes / backslashes / control chars)."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


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
    path = Path(path) if path else (Path(os.environ[CONFIG_ENV]) if CONFIG_ENV in os.environ else DEFAULT_CONFIG)
    if path.exists():
        text = path.read_text(encoding="utf-8")
    else:
        text = _default_toml_text()
    text = _upsert_key(text, f"backends.{name}", "api_key", _toml_string(key))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    # The file now holds a credential (api_key); keep it readable only by the owner.
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
