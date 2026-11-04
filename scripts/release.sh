#!/usr/bin/env bash
# =============================================================================
# release.sh - litcli semi-automatic release (gated, non-interactive)
#
# Flow, mirroring qbtt's release.sh but for a pip package hosted on Gitee:
#   1) gates (any failure -> exit 1, nothing is written)
#      ① on master and level with origin        ② worktree clean
#      ③ full test suite green                  ④ ruff clean
#      ⑤ package builds (sdist + wheel)         ⑥ CHANGELOG [Unreleased] non-empty
#      ⑦ target version not tagged yet          ⑧ __version__ == target
#   2) bump `litcli/_version.py` (--bump major|minor|patch, default patch; `none` releases the
#      current __version__ in place) and harvest CHANGELOG [Unreleased] into a dated section
#   3) commit `chore(release): vX.Y.Z` + tag vX.Y.Z
#   4) build dist/ artifacts + SHA256SUMS
#   5) only with --push: push master + tag, and create the Gitee Release with the artifacts
#      attached when GITEE_TOKEN is set (otherwise print the manual steps)
#
# Two-phase on purpose: the gates and the local artifacts are cheap and reversible; publishing
# outward needs --push. `--check` runs the gates only and changes nothing.
#
# Usage:
#   scripts/release.sh --check                                  gates only, change nothing
#   scripts/release.sh --dry-run                                gates + show what would happen
#   scripts/release.sh [--bump patch|minor|major|none] [--notes <file.md>] [--push] [--no-release]
#
# Environment:
#   GITEE_TOKEN   Gitee personal access token (releases scope). Without it --push still pushes
#                 the tag; the Release step is then skipped with the manual steps printed.
# =============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

VERSION_FILE="litcli/_version.py"
BRANCH_REQUIRED="master"
GITEE_API="https://gitee.com/api/v5"
# owner/repo from git@gitee.com:owner/repo.git (strip .git first; a lazy regex swallows it)
GITEE_PROJECT="$(git remote get-url origin | sed -E 's#\.git$##; s#^.*[:/]([^/]+/[^/]+)$#\1#')"

CHECK_ONLY=0
DRY_RUN=0
BUMP="patch"
NOTES_FILE=""
DO_PUSH=0
DO_RELEASE=1

while [ $# -gt 0 ]; do
  case "$1" in
    --check) CHECK_ONLY=1 ;;
    --dry-run) DRY_RUN=1 ;;
    --bump) BUMP="${2:?--bump needs an argument}"; shift ;;
    --notes) NOTES_FILE="${2:?--notes needs a path}"; shift ;;
    --push) DO_PUSH=1 ;;
    --no-release) DO_RELEASE=0 ;;
    -h|--help) sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done
case "$BUMP" in major|minor|patch|none) ;; *) echo "--bump must be major|minor|patch|none" >&2; exit 2 ;; esac

# The target version is resolved up front so gate ⑦ tests what will actually be tagged.
VER="$(python3 -c "import re,pathlib; print(re.search(r'__version__ = \"(.*)\"', pathlib.Path('$VERSION_FILE').read_text()).group(1))")"
bump_ver() {
  python3 - "$VER" "$1" <<'PY'
import sys
if sys.argv[2] == "none":
    print(sys.argv[1]); raise SystemExit
v = [int(x) for x in sys.argv[1].split(".")]
if sys.argv[2] == "major": v = [v[0] + 1, 0, 0]
elif sys.argv[2] == "minor": v = [v[0], v[1] + 1, 0]
else: v = [v[0], v[1], v[2] + 1]
print(".".join(map(str, v)))
PY
}
NEW_VER="$(bump_ver "$BUMP")"

fail=""
echo "==> [1/6] gates"

echo "  - branch and sync state ..."
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if [ "$BRANCH" = "$BRANCH_REQUIRED" ]; then echo "  ✓ on $BRANCH"; else
  echo "  ✗ on $BRANCH, releases are cut from $BRANCH" >&2; fail="$fail,branch"; fi
if git rev-parse --verify --quiet "origin/$BRANCH_REQUIRED" >/dev/null; then
  if [ "$(git rev-parse HEAD)" = "$(git rev-parse "origin/$BRANCH_REQUIRED")" ]; then
    echo "  ✓ level with origin/$BRANCH_REQUIRED"
  else
    echo "  ✗ HEAD differs from origin/$BRANCH_REQUIRED (push or pull first)" >&2; fail="$fail,sync"
  fi
fi

echo "  - worktree clean ..."
if [ -z "$(git status --porcelain --untracked-files=normal)" ]; then
  echo "  ✓ worktree clean"
else
  echo "  ✗ worktree has changes (release.sh commits only the version and the CHANGELOG)" >&2
  fail="$fail,worktree"
fi

echo "  - test suite ..."
if python3 -m unittest discover -s tests >/dev/null 2>&1; then echo "  ✓ tests green"; else
  echo "  ✗ tests failed" >&2; fail="$fail,tests"; fi

echo "  - lint (ruff) ..."
if ruff check litcli tests >/dev/null 2>&1; then echo "  ✓ lint clean"; else
  echo "  ✗ lint findings, run: ruff check litcli tests" >&2; fail="$fail,lint"; fi

# A build failure must surface here, not after the version has been bumped and committed.
echo "  - package builds ..."
BUILD_DIR="$(mktemp -d)"
if python3 -m build --no-isolation --outdir "$BUILD_DIR" >/dev/null 2>&1; then
  echo "  ✓ sdist + wheel build"
else
  echo "  ✗ build failed, run: python3 -m build --no-isolation" >&2; fail="$fail,build"
fi
rm -rf "$BUILD_DIR"

# The [Unreleased] section ends at the next H2 of any kind; a hand-added `## Notes` must not be swallowed.
echo "  - CHANGELOG [Unreleased] ..."
if [ -f CHANGELOG.md ] && awk '/^## \[Unreleased\]/{f=1;next}/^## /{f=0}f && /^[-*]/ {found=1} END{exit !found}' CHANGELOG.md; then
  echo "  ✓ [Unreleased] has entries"
else
  echo "  ✗ CHANGELOG [Unreleased] empty or missing" >&2; fail="$fail,changelog"
fi

echo "  - version and tag ..."
echo "  - current __version__ = $VER, release target v$NEW_VER"
if [ "$BUMP" = none ] && [ "$NEW_VER" != "$VER" ]; then
  echo "  ✗ --bump none resolves to a different version" >&2; fail="$fail,version"
fi
if git rev-parse --verify --quiet "refs/tags/v$NEW_VER" >/dev/null; then
  echo "  ✗ v$NEW_VER already tagged (bump further)" >&2; fail="$fail,tag"
else
  echo "  ✓ v$NEW_VER not tagged yet"
fi

[ -z "$fail" ] || { echo "==> gates failed:${fail#,}" >&2; exit 1; }
echo "==> gates green ✓"
[ "$CHECK_ONLY" = 1 ] && { echo "(--check: nothing changed)"; exit 0; }

TODAY="$(date +%F)"
if [ "$BUMP" = none ]; then
  echo "==> [2/6] in-place release v$VER (version unchanged) + harvest CHANGELOG"
else
  echo "==> [2/6] bump $VER -> $NEW_VER + harvest CHANGELOG"
fi

if [ "$DRY_RUN" = 1 ]; then
  echo "  (dry-run) $VERSION_FILE -> $NEW_VER"
  echo "  (dry-run) CHANGELOG [Unreleased] -> [$NEW_VER] - $TODAY, [Unreleased] re-opened"
  echo "  (dry-run) commit + tag v$NEW_VER + build dist/ (+ push and Gitee Release with --push)"
  echo "==> dry-run done, nothing written"
  exit 0
fi

if [ "$BUMP" != none ]; then
  python3 - "$VERSION_FILE" "$NEW_VER" <<'PY'
import re, sys
path, ver = sys.argv[1], sys.argv[2]
s = open(path, encoding="utf-8").read()
open(path, "w", encoding="utf-8").write(re.sub(r'__version__ = ".*"', f'__version__ = "{ver}"', s))
PY
fi

# Harvest: [Unreleased] becomes the dated version section, above the newest existing version;
# an empty [Unreleased] is re-opened at the tail (the file stays newest-first).
python3 - CHANGELOG.md "$NEW_VER" "$TODAY" <<'PY'
import re, sys
path, ver, today = sys.argv[1], sys.argv[2], sys.argv[3]
s = open(path, encoding="utf-8").read()
m = re.search(r"^## \[Unreleased\]\n(.*?)(?=^## (?!\[Unreleased\])|\Z)", s, re.M | re.S)
if not m:
    sys.exit("CHANGELOG: [Unreleased] section missing")
body = re.sub(r"^## \[Unreleased\]\n", "", m.group(1)).strip("\n") + "\n\n"
header = s[: m.start()]
# a stray empty [Unreleased] left at EOF by an older harvest is trailing content, not this section
rest = s[m.end():].rstrip()
while rest.endswith("## [Unreleased]"):
    rest = rest[: -len("## [Unreleased]")].rstrip()
tail = (rest + "\n\n") if rest else ""
section = f"## [{ver}] - {today}\n\n{body}"
first_ver = re.search(r"^## \[(?!Unreleased\])", header, re.M)
if first_ver:
    out = header[: first_ver.start()] + section + header[first_ver.start():] + tail + "## [Unreleased]\n\n"
else:
    out = header + section + tail + "## [Unreleased]\n\n"
open(path, "w", encoding="utf-8").write(out)
print(f"  CHANGELOG: harvested as [{ver}] - {today}")
PY

echo "==> [3/6] commit + tag v$NEW_VER"
git add "$VERSION_FILE" CHANGELOG.md
if [ "$BUMP" = none ]; then
  git commit -q -m "chore(release): cut release v$NEW_VER, harvest changelog"
else
  git commit -q -m "chore(release): bump to $NEW_VER, harvest changelog"
fi
git tag "v$NEW_VER"
echo "  ✓ commit + tag v$NEW_VER (local)"

echo "==> [4/6] build dist/ artifacts"
rm -rf dist
python3 -m build --no-isolation --outdir dist >/dev/null
( cd dist && sha256sum ./* > SHA256SUMS )
echo "  ✓ dist/ built: $(cd dist && ls | tr '\n' ' ')"

if [ "$DO_PUSH" = 0 ]; then
  echo "==> [5/6] push skipped (no --push); tag is local"
  echo "    later: git push origin $BRANCH_REQUIRED && git push origin v$NEW_VER"
  echo "    note: re-running after tagging trips gate ⑦; push by hand instead."
  echo "==> [6/6] release skipped"
  exit 0
fi

echo "==> [5/6] push $BRANCH_REQUIRED + v$NEW_VER"
git push origin "$BRANCH_REQUIRED"
git push origin "v$NEW_VER"
echo "  ✓ pushed"

if [ "$DO_RELEASE" = 0 ]; then
  echo "==> [6/6] release skipped (--no-release)"; exit 0
fi

echo "==> [6/6] Gitee Release"
if [ -n "$NOTES_FILE" ]; then NOTES="$(cat "$NOTES_FILE")"; else
  NOTES="$(awk "/^## \[$NEW_VER\]/{f=1;next}/^## \[/{f=0}f" CHANGELOG.md)"; fi

if [ -z "${GITEE_TOKEN:-}" ]; then
  echo "  (GITEE_TOKEN unset: create the Release by hand at"
  echo "   https://gitee.com/$GITEE_PROJECT/releases, paste the CHANGELOG [$NEW_VER] section,"
  echo "   attach dist/* and dist/SHA256SUMS)"
  exit 0
fi

PAYLOAD="$(mktemp)"
trap 'rm -f "$PAYLOAD"' EXIT
NOTES="$NOTES" python3 - "$PAYLOAD" "$NEW_VER" <<'PY'
import json, os, sys
json.dump({"tag_name": f"v{sys.argv[2]}", "name": f"litcli v{sys.argv[2]}",
           "body": os.environ["NOTES"], "target_commitish": "master", "prerelease": False},
          open(sys.argv[1], "w", encoding="utf-8"), ensure_ascii=False)
PY
curl -sS -f -X POST "$GITEE_API/repos/$GITEE_PROJECT/releases" \
  -H "Content-Type: application/json" -H "Authorization: token $GITEE_TOKEN" \
  --data-binary "@$PAYLOAD" > "$PAYLOAD.out" || {
    echo "  ✗ Release creation failed (tag is pushed; fix and re-POST by hand)" >&2; exit 1; }
RELEASE_ID="$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['id'])" "$PAYLOAD.out")"
rm -f "$PAYLOAD.out"
echo "  ✓ Release v$NEW_VER created (id $RELEASE_ID)"
for f in dist/*; do
  curl -sS -f -X POST "$GITEE_API/repos/$GITEE_PROJECT/releases/$RELEASE_ID/attach_files" \
    -H "Authorization: token $GITEE_TOKEN" -F "file=@$f" >/dev/null \
    && echo "  ✓ attached $(basename "$f")" || echo "  ✗ attach failed: $(basename "$f")" >&2
done
echo "==> done: v$NEW_VER"
