"""``python -m litcli`` — the module-entry twin of the installed ``litcli`` console script."""
import sys

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
