"""Entry script for the frozen (binary) builds.

PyInstaller needs a script to analyse; the wheel keeps using the ``litcli.cli:main`` console
script. Both land in the same ``main()``.
"""
from litcli.cli import main

raise SystemExit(main())
