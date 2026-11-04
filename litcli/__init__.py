"""litcli — a general academic CLI with pluggable multi-backend support.

The domain layer (models/citation/pdf) is independent of any reference manager;
the backend layer (backends/) distinguishes "read" / "read+write" / "attachable" via the
Backend abstraction + capabilities;
config instantiates backends from ~/.config/litcli/config.toml.
"""

from ._version import __version__ as __version__
