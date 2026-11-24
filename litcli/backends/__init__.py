"""Pluggable backends: the BACKENDS registry (see config.discover_backends)."""
from __future__ import annotations

from importlib import import_module

#: Reader modules already imported (``None`` = it failed), and why each one failed.
_modules: dict[str, object] = {}
import_errors: dict[str, str] = {}


def load_module(name: str):
    """Import ``litcli.backends.<name>`` once; ``None`` when it cannot be imported.

    A missing module raises ``ImportError``, but "not ready" also covers a syntax error, a bad
    constant and a renamed attribute — none of them an ``ImportError`` — so anything the import
    raises is caught (`KeyboardInterrupt`/`SystemExit` aside). The reason lands in
    :data:`import_errors`: the swallow would otherwise surface only as "unknown backend type",
    a name the config plainly mentions.
    """
    if name not in _modules:
        try:
            _modules[name] = import_module(f".{name}", __package__)
        except Exception as e:
            import_errors[name] = f"{type(e).__name__}: {e}"
            _modules[name] = None
    return _modules[name]
