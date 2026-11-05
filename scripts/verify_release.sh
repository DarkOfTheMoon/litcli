#!/usr/bin/env bash
# =============================================================================
# verify_release.sh - check a published release the way a consumer meets it
#
# The release gates run in the maintainer's tree, so they prove the code builds *here*. A
# published Release is a different object: uploads can be truncated, a wheel can be built from
# another commit, a console entry point can be missing from the artifact. This script downloads
# the Release assets anonymously, checks them against the published SHA256SUMS, installs both
# the wheel and the sdist into a throwaway venv, and runs real commands from that venv.
#
# Usage:
#   scripts/verify_release.sh [vX.Y.Z]     version defaults to the latest tag on HEAD
#
# Exit 0 = the Release is consumable. Anything else = a concrete defect to fix (or re-upload).
# Needs network; the venv install pulls dependencies.
# =============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

GITEE_API="https://gitee.com/api/v5"
GITEE_PROJECT="$(git remote get-url origin | sed -E 's#\.git$##; s#^.*[:/]([^/]+/[^/]+)$#\1#')"

VER="${1:-}"
if [ -z "$VER" ]; then
  VER="$(git describe --tags --exact-match HEAD 2>/dev/null || git describe --tags --abbrev=0)"
fi
VER="${VER#v}"
git rev-parse --verify --quiet "refs/tags/v$VER" >/dev/null || { echo "v$VER is not a tag" >&2; exit 2; }

WORK="$(mktemp -d)"
VENV="$(mktemp -d)/venv"
cleanup() { rm -rf "$WORK" "$(dirname "$VENV")"; }
trap cleanup EXIT

step() { printf '  - %s ... ' "$1"; }
ok() { echo "ok"; }
bad() { echo "FAILED"; }

echo "==> verifying release v$VER of $GITEE_PROJECT"
step "release exists and is published"
META="$WORK/release.json"
curl -sS -f "$GITEE_API/repos/$GITEE_PROJECT/releases/tags/v$VER" -o "$META" || {
  bad; echo "no published release for v$VER" >&2; exit 1; }
ok

step "assets: wheel + sdist + SHA256SUMS"
python3 - "$META" <<'PY'
import json, sys
assets = [a["name"] for a in json.load(open(sys.argv[1])).get("assets") or []]
want = {"SHA256SUMS"}
want |= {a for a in assets if a.endswith(".whl")}
want |= {a for a in assets if a.endswith(".tar.gz") and "litcli-" in a}
missing = want - set(assets)
sys.exit(f"missing assets: {sorted(missing)}" if missing else 0)
PY
ok

step "download"
python3 - "$META" "$WORK" <<'PY'
import json, sys, urllib.request
meta, work = sys.argv[1], sys.argv[2]
for asset in json.load(open(meta)).get("assets") or []:
    name = asset["name"]
    if name.endswith("SHA256SUMS") or name.endswith(".whl") or name.startswith("litcli-"):
        urllib.request.urlretrieve(asset["browser_download_url"], f"{work}/{name}")
PY
ok

step "SHA256SUMS matches every artifact"
( cd "$WORK" && sha256sum -c --quiet SHA256SUMS ) || { bad; exit 1; }
ok

step "version in the artifact matches the tag"
WHEEL="$WORK/$(basename "$(ls "$WORK"/*.whl)")"
case "$WHEEL" in *"-$VER-"*) ok ;; *) bad; echo "$WHEEL does not carry $VER" >&2; exit 1 ;; esac

step "wheel installs into a clean venv"
python3 -m venv "$VENV" >/dev/null
"$VENV/bin/pip" install -q "$WHEEL" || { bad; exit 1; }
ok

step "console entry point works from the installed package"
out="$("$VENV/bin/litcli" --version)" || { bad; exit 1; }
case "$out" in "litcli $VER") ok ;; *) bad; echo "unexpected --version output: $out" >&2; exit 1 ;; esac

step "a real backend answers (crossref)"
"$VENV/bin/litcli" search --backend crossref "deep learning" --limit 3 --format json >/dev/null \
  || { bad; exit 1; }
ok

step "sdist builds and installs"
SDIST="$WORK/$(basename "$(ls "$WORK"/litcli-"$VER"*.tar.gz | head -1)")"
"$VENV/bin/pip" install -q --force-reinstall "$SDIST" || { bad; exit 1; }
ok

echo "==> v$VER is consumable"
