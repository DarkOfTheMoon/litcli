#!/bin/sh
# litcli @VERSION@ — self-extracting installer for Linux.
#
# Everything below the __PAYLOAD__ marker is a tar.gz of the frozen build; the script unpacks
# it under @PREFIX@/opt/litcli-@VERSION@ and links @PREFIX@/bin/litcli. No Python is needed.
#
#   sh litcli-@VERSION@-linux-x86_64.run [options]
#
#     --prefix DIR     install root (default: $LITCLI_PREFIX or $HOME/.local)
#     --with-browser   also download Chromium for the cnki/douban backends (~150 MB)
#     -y, --yes        no prompts
#     --uninstall      remove an installed copy instead of installing
#     -h, --help       this text
set -eu

VERSION="@VERSION@"
PREFIX="${LITCLI_PREFIX:-$HOME/.local}"
DEST="$PREFIX/opt/litcli-$VERSION"
LINK="$PREFIX/bin/litcli"
WITH_BROWSER=0
ASSUME_YES=0
ACTION=install

say() { printf '%s\n' "$*"; }
die() { printf '[error] %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        --prefix) [ $# -ge 2 ] || die "--prefix needs a directory"; PREFIX="$2"; DEST="$PREFIX/opt/litcli-$VERSION"; LINK="$PREFIX/bin/litcli"; shift 2 ;;
        --with-browser) WITH_BROWSER=1; shift ;;
        -y|--yes) ASSUME_YES=1; shift ;;
        --uninstall) ACTION=uninstall; shift ;;
        -h|--help) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "unknown option: $1 (try --help)" ;;
    esac
done

if [ "$ACTION" = uninstall ]; then
    removed=0
    for dir in "$PREFIX"/opt/litcli-*; do
        [ -d "$dir" ] || continue
        rm -rf "$dir"
        removed=1
    done
    [ "$removed" = 1 ] || die "nothing installed under $PREFIX/opt"
    [ -L "$LINK" ] && rm -f "$LINK"
    rmdir "$PREFIX/opt" 2>/dev/null || true
    say "litcli removed from $PREFIX (downloaded browsers stay in the Playwright cache)"
    exit 0
fi

[ -r "$0" ] || die "run the installer from a file, not a pipe"
[ -x /usr/bin/env ] || die "a POSIX shell and env(1) are required"

if [ "$ASSUME_YES" = 0 ] && [ -t 0 ]; then
    printf 'install litcli %s to %s? [Y/n] ' "$VERSION" "$DEST"
    read -r answer || answer=""
    case "$answer" in [Nn]*) die "cancelled" ;; esac
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT INT TERM

say "unpacking …"
start="$(awk '/^__PAYLOAD__$/{print NR+1; exit}' "$0")"
tail -n +"$start" "$0" | tar -xz -C "$tmp" || die "payload is damaged (re-download the installer)"

mkdir -p "$PREFIX/opt" "$PREFIX/bin"
rm -rf "$DEST"
mv "$tmp/litcli" "$DEST"
ln -sfn "$DEST/litcli" "$LINK"

say "litcli $VERSION installed: $DEST"
say "launcher: $LINK"
case ":$PATH:" in
    *":$PREFIX/bin:"*) ;;
    *) say "note: $PREFIX/bin is not on PATH — add it with: export PATH=\"$PREFIX/bin:\$PATH\"" ;;
esac

if [ "$WITH_BROWSER" = 1 ]; then
    say "downloading Chromium for the cnki/douban backends …"
    "$DEST/litcli" backends install-browser || say "browser download failed; retry later with: litcli backends install-browser"
else
    say "cnki/douban need a browser: rerun with --with-browser, or later run: litcli backends install-browser"
fi
say "upgrade by running a newer installer; uninstall with: sh $0 --uninstall"
exit 0

__PAYLOAD__
