"""Single source of truth for the package version.

pyproject.toml reads this via `[tool.setuptools.dynamic] version = {attr = "litcli._version.__version__"}`,
so bumping the version means editing exactly this one string.
"""

__version__ = "0.3.0"
