#!/bin/sh
# Build the Linux binary installer (dist/bin/litcli-<ver>-linux-<arch>.run).
#
#   scripts/build_binary.sh [--keep]
#
# Freezes litcli with PyInstaller into a throwaway venv, then appends the tree as a tar.gz
# payload to packaging/installer/linux-header.sh. The venv is created from scratch so the
# bundle carries exactly the declared dependencies, never this machine's extras.
# `--keep` leaves build/pyinstaller and build/stage for inspection.
#
# The frozen build has no .py sources: the entry point, the package and its dependencies are
# bytecode and shared objects under a single directory the installer extracts.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
VERSION="$(python3 -c "import re,pathlib; print(re.search(r'__version__ = \"(.*)\"', pathlib.Path('litcli/_version.py').read_text()).group(1))")"
ARCH="$(uname -m)"
KEEP=0
[ "${1:-}" = "--keep" ] && KEEP=1

WORK="$ROOT/build/pyinstaller"
STAGE="$ROOT/build/stage"
OUT="$ROOT/dist/bin/litcli-$VERSION-linux-$ARCH.run"
VENV="$WORK/venv"

command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }
[ "$(uname -s)" = Linux ] || { echo "this script builds the Linux installer; use build_binary.ps1 on Windows" >&2; exit 1; }

echo "==> clean"
rm -rf "$WORK" "$STAGE"
mkdir -p "$WORK" "$STAGE" "$(dirname "$OUT")"

echo "==> build environment"
python3 -m venv "$VENV"
"$VENV/bin/pip" -q install --upgrade pip
"$VENV/bin/pip" -q install ".[cnki,dev]"   # [cnki]: playwright + jieba, bundled; [dev] pins pyinstaller

echo "==> freeze (PyInstaller, onedir)"
"$VENV/bin/pyinstaller" --noconfirm --clean --distpath "$STAGE" --workpath "$WORK/pyinstaller" \
    packaging/litcli.spec >"$WORK/pyinstaller.log" 2>&1 \
    || { tail -40 "$WORK/pyinstaller.log" >&2; echo "pyinstaller failed (full log: $WORK/pyinstaller.log)" >&2; exit 1; }

echo "==> smoke-test the frozen tree"
"$STAGE/litcli/litcli" --version | grep -qx "litcli $VERSION" \
    || { echo "frozen build does not answer --version" >&2; exit 1; }
"$STAGE/litcli/litcli" backends install-browser --help >/dev/null
# Backends are imported by name at run time, which is what the spec's hiddenimports is for.
# Ask the registry for every one of them: a module that did not survive the freeze is listed
# as ``[unavailable]``, and a config that failed to load would print a single line instead.
"$VENV/bin/python" - "$WORK/backends.toml" <<'PY'
import sys
from litcli.config import _registry
with open(sys.argv[1], "w") as f:
    for n in sorted(_registry()):
        f.write(f'[backends.{n}]\ntype = "{n}"\n')
PY
backends_out="$(LITCLI_CONFIG="$WORK/backends.toml" "$STAGE/litcli/litcli" backends list)"
if printf '%s\n' "$backends_out" | grep -q '\[unavailable\]'; then
    printf '%s\n' "$backends_out" | grep '\[unavailable\]' >&2
    echo "the frozen build cannot import the backends above (spec hiddenimports?)" >&2
    exit 1
fi
want="$("$VENV/bin/python" -c 'from litcli.config import _registry; print(len(_registry()))')"
have="$(printf '%s\n' "$backends_out" | grep -c .)"
[ "$have" = "$want" ] \
    || { echo "backends list printed $have rows for $want backends" >&2; exit 1; }

echo "==> assemble $OUT"
# Both placeholders are filled: @PREFIX@ with the runtime shell variable the header defines
# (its value is chosen at install time), @VERSION@ with this build's version.
sed -e "s/@VERSION@/$VERSION/g" -e 's|@PREFIX@|$PREFIX|g' \
    packaging/installer/linux-header.sh >"$OUT"
# A leftover placeholder would ship in the built-in help; the payload is binary, so only the
# script part above the marker is inspected.
awk '/^__PAYLOAD__$/{exit} {print}' "$OUT" | grep -q '@' \
    && { echo "unsubstituted @PLACEHOLDER@ left in $OUT" >&2; exit 1; }
tar -C "$STAGE" -czf "$WORK/payload.tar.gz" litcli
cat "$WORK/payload.tar.gz" >>"$OUT"
chmod +x "$OUT"

echo "==> verify the installer (fresh prefix, no prompts)"
TMP_PREFIX="$WORK/prefix"
sh "$OUT" --prefix "$TMP_PREFIX" --yes >/dev/null
"$TMP_PREFIX/bin/litcli" --version | grep -qx "litcli $VERSION" \
    || { echo "installed binary does not run" >&2; exit 1; }
"$TMP_PREFIX/bin/litcli" search --help >/dev/null
sh "$OUT" --prefix "$TMP_PREFIX" --uninstall >/dev/null
[ ! -e "$TMP_PREFIX/opt" ] || { echo "uninstall left files behind" >&2; exit 1; }

[ "$KEEP" = 1 ] || rm -rf "$WORK/prefix"
echo "==> done: $OUT ($(du -h "$OUT" | cut -f1))"
