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
#   4) build dist/ artifacts + SHA256SUMS ([--binary] also builds this platform's installer)
#   5) only with --push: push master + tag to every remote, and create the Gitee Release with the
#      artifacts attached when a token is configured (otherwise print the manual steps)
#      --binary releases need a Linux *and* a Windows installer. The local build covers one
#      platform; the other is fetched from the GitHub CI artifact of the pushed commit (or
#      supplied with --installer from a Windows machine). Publishing stops if either is absent,
#      because a one-platform release is worse than none: the installer list would silently lie.
#   6) consumer check: download the published assets back and install them in a throwaway venv
#      (scripts/verify_release.sh) — skipped when no Release was created
#
# Two-phase on purpose: the gates and the local artifacts are cheap and reversible; publishing
# outward needs --push. `--check` runs the gates only and changes nothing.
#
# Usage:
#   scripts/release.sh --check                                  gates only, change nothing
#   scripts/release.sh --dry-run                                gates + show what would happen
#   scripts/release.sh [--bump patch|minor|major|none] [--notes <file.md>] [--push] [--no-release]
#                      [--binary [--installer <path>]...]
#   scripts/release.sh --release-only [vX.Y.Z]                  (re)create the Release of that tag
#                                                               (default: the tag on HEAD); nothing is
#                                                               bumped, committed or pushed
#
# Environment:
#   GITEE_TOKEN   Gitee personal access token (releases scope), falls back to
#                 ~/.config/litcli/gitee.token (chmod 600, never committed). Without it --push still
#                 pushes the tag; the Release step is then skipped with the manual steps printed.
# =============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

VERSION_FILE="litcli/_version.py"
BRANCH_REQUIRED="master"
GITEE_API="https://gitee.com/api/v5"
# owner/repo from git@gitee.com:owner/repo.git (strip .git first; a lazy regex swallows it)
GITEE_PROJECT="$(git remote get-url origin | sed -E 's#\.git$##; s#^.*[:/]([^/]+/[^/]+)$#\1#')"
# CI mirror (the side that builds the other platform's installer); empty when not configured.
GITHUB_PROJECT="$(git remote get-url github 2>/dev/null | sed -E 's#\.git$##; s#^.*[:/]([^/]+/[^/]+)$#\1#')" || GITHUB_PROJECT=""

missing_installers() {
  # The platforms a --binary release must ship; prints the ones not in dist/bin yet.
  ls dist/bin/*-linux-*.run >/dev/null 2>&1 || echo linux
  ls dist/bin/*-windows-*-setup.exe >/dev/null 2>&1 || echo windows
}

fetch_ci_installer() {
  # The Windows installer can only be built on Windows; take it from the CI run of HEAD.
  local sha="$1" id attempt
  echo "==> [5b/7] waiting for CI to build the Windows installer"
  for attempt in $(seq 1 60); do
    id="$(gh run list --repo "$GITHUB_PROJECT" --commit "$sha" --limit 1 \
          --json databaseId --jq '.[0].databaseId // empty' 2>/dev/null || true)"
    [ -n "$id" ] && break
    sleep 10
  done
  if [ -z "${id:-}" ]; then
    echo "  ✗ CI did not pick up $sha; is the workflow enabled on $GITHUB_PROJECT?" >&2
    return 1
  fi
  echo "  run: https://github.com/$GITHUB_PROJECT/actions/runs/$id"
  gh run watch "$id" --repo "$GITHUB_PROJECT" --exit-status >/dev/null \
    || { echo "  ✗ CI run $id failed; see the link above" >&2; return 1; }
  gh run download "$id" --repo "$GITHUB_PROJECT" --name installer-windows-latest --dir dist/bin
}

publish_release() {
  local ver="$1" notes
  echo "==> Gitee Release v$ver"
  if [ -n "$NOTES_FILE" ]; then notes="$(cat "$NOTES_FILE")"; else
    notes="$(awk "/^## \\[$ver\\]/{f=1;next}/^## \\[/{f=0}f" CHANGELOG.md)"; fi
  if [ -z "${GITEE_TOKEN:-}" ]; then
    echo "  (no GITEE_TOKEN / ~/.config/litcli/gitee.token: create the Release by hand at"
    echo "   https://gitee.com/$GITEE_PROJECT/releases, paste the CHANGELOG [$ver] section,"
    echo "   attach dist/*, dist/bin/* (binary installers) and dist/SHA256SUMS)"
    return 0
  fi

  local payload resp id f
  payload="$(mktemp)"; resp="$(mktemp)"
  trap 'rm -f "$payload" "$resp"' RETURN
  NOTES="$notes" python3 - "$payload" "$ver" <<'PAYLOAD'
import json, os, sys
json.dump({"tag_name": f"v{sys.argv[2]}", "name": f"litcli v{sys.argv[2]}",
           "body": os.environ["NOTES"], "target_commitish": "master", "prerelease": False},
          open(sys.argv[1], "w", encoding="utf-8"), ensure_ascii=False)
PAYLOAD
  curl -sS -f -X POST "$GITEE_API/repos/$GITEE_PROJECT/releases" \
    -H "Content-Type: application/json" -H "Authorization: token $GITEE_TOKEN" \
    --data-binary "@$payload" > "$resp" || {
      echo "  ✗ Release creation failed (is the tag pushed? does the token have the release scope?)" >&2
      return 1; }
  id="$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['id'])" "$resp")"
  echo "  ✓ Release v$ver created (id $id)"
  for f in dist/* dist/bin/*; do
    [ -f "$f" ] || continue
    curl -sS -f -X POST "$GITEE_API/repos/$GITEE_PROJECT/releases/$id/attach_files" \
      -H "Authorization: token $GITEE_TOKEN" -F "file=@$f" >/dev/null \
      && echo "  ✓ attached $(basename "$f")" || echo "  ✗ attach failed: $(basename "$f")" >&2
  done
}

CHECK_ONLY=0
DRY_RUN=0
RELEASE_ONLY=0
BUMP="patch"
NOTES_FILE=""
DO_PUSH=0
DO_RELEASE=1
DO_BINARY=0
EXTRA_INSTALLERS=()

while [ $# -gt 0 ]; do
  case "$1" in
    --check) CHECK_ONLY=1 ;;
    --dry-run) DRY_RUN=1 ;;
    --release-only) RELEASE_ONLY=1
      case "${2:-}" in v[0-9]*|[0-9]*) REL_VER="${2#v}"; shift ;; esac ;;
    --bump) BUMP="${2:?--bump needs an argument}"; shift ;;
    --notes) NOTES_FILE="${2:?--notes needs a path}"; shift ;;
    --push) DO_PUSH=1 ;;
    --no-release) DO_RELEASE=0 ;;
    --binary) DO_BINARY=1 ;;
    --installer) DO_BINARY=1; EXTRA_INSTALLERS+=("${2:?--installer needs a path}"); shift ;;
    -h|--help) sed -n '2,39p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done
case "$BUMP" in major|minor|patch|none) ;; *) echo "--bump must be major|minor|patch|none" >&2; exit 2 ;; esac

# Secret lookup: environment first, then the untracked per-user file (600).
if [ -z "${GITEE_TOKEN:-}" ] && [ -f "${HOME}/.config/litcli/gitee.token" ]; then
  GITEE_TOKEN="$(tr -d '\n' < "${HOME}/.config/litcli/gitee.token")"
fi

# Re-attach a Release to a tag that already exists (the run that cut it had no token, the token
# was missing the scope, or the upload half-failed). Nothing is bumped, committed or pushed.
if [ "$RELEASE_ONLY" = 1 ]; then
  if [ -z "${REL_VER:-}" ]; then
    REL_VER="$(git describe --tags --exact-match HEAD 2>/dev/null || git describe --tags --abbrev=0)"
  fi
  REL_VER="${REL_VER#v}"
  git rev-parse --verify --quiet "refs/tags/v$REL_VER" >/dev/null || {
    echo "--release-only: v$REL_VER is not a tag" >&2; exit 1; }
  echo "==> release-only: v$REL_VER"
  git ls-remote --tags --exit-code origin "refs/tags/v$REL_VER" >/dev/null 2>&1 || {
    echo "  ✗ v$REL_VER is not pushed yet: git push origin v$REL_VER" >&2; exit 1; }
  echo "  ✓ tag is pushed"
  if [ ! -d dist ] || [ -z "$(ls -A dist 2>/dev/null)" ]; then
    echo "  - dist/ missing, rebuilding ..."
    python3 -m build --no-isolation --outdir dist >/dev/null
    echo "  ✓ dist/ rebuilt (installers are only attached if already in dist/bin/)"
  fi
  # A Release that lists installers must list both platforms; top up from CI before publishing.
  if ls dist/bin/*-* >/dev/null 2>&1; then
    if [ -n "$(missing_installers)" ] && [ -n "$GITHUB_PROJECT" ] && command -v gh >/dev/null 2>&1; then
      fetch_ci_installer "$(git rev-parse "v$REL_VER^{commit}")" || exit 1
    fi
    [ -z "$(missing_installers)" ] || {
      echo "  ✗ release has installers but misses: $(missing_installers | tr '\n' ' ')" >&2; exit 1; }
  fi
  ( cd dist && { sha256sum ./* bin/* 2>/dev/null | while read -r h f; do printf '%s  %s\n' "$h" "${f##*/}"; done; } > SHA256SUMS )
  publish_release "$REL_VER" || exit 1
  echo "==> done: v$REL_VER"
  exit 0
fi

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

# Harvest: [Unreleased] becomes a dated version section, with a fresh empty [Unreleased] kept above
# it (the file stays newest-first; the oldest version ends up at the bottom).
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
open(path, "w", encoding="utf-8").write(
    header + "## [Unreleased]\n\n" + f"## [{ver}] - {today}\n\n" + body + tail)
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
if [ "$DO_BINARY" = 1 ]; then
  # Only this platform's installer can be built here. The other one comes from --installer or,
  # failing that, from the CI run of this very commit (fetched after the push, step 5b).
  mkdir -p dist/bin
  case "$(uname -s)" in
    Linux) sh scripts/build_binary.sh ;;
    *) echo "  ! no local binary build on $(uname -s); both installers must be supplied or fetched" ;;
  esac
  for p in ${EXTRA_INSTALLERS[@]+"${EXTRA_INSTALLERS[@]}"}; do
    cp "$p" dist/bin/ || { echo "  ✗ cannot copy --installer $p" >&2; exit 1; }
    echo "  ✓ added $(basename "$p")"
  done
fi
( cd dist && { sha256sum ./* bin/* 2>/dev/null | while read -r h f; do printf '%s  %s\n' "$h" "${f##*/}"; done; } > SHA256SUMS )
echo "  ✓ dist/ built: $(cd dist && ls | tr '\n' ' ')"

if [ "$DO_PUSH" = 0 ]; then
  echo "==> [5/6] push skipped (no --push); tag is local"
  echo "    later: git push origin $BRANCH_REQUIRED && git push origin v$NEW_VER"
  echo "    note: re-running after tagging trips gate ⑦; push by hand instead."
  if [ "$DO_BINARY" = 1 ] && [ -n "$(missing_installers)" ]; then
    echo "    note: installers still missing ($(missing_installers | tr '\n' ' ')) — they are fetched"
    echo "          from CI at publish time when you run --release-only after pushing"
  fi
  echo "==> [6/6] release skipped"
  exit 0
fi

echo "==> [5/6] push $BRANCH_REQUIRED + v$NEW_VER"
# Every configured remote gets the branch and the tag; a mirror (e.g. the GitHub side that runs
# CI) must not silently lag behind what was released.
REMOTES="origin"
git remote get-url github >/dev/null 2>&1 && REMOTES="$REMOTES github"
for r in $REMOTES; do
  git push "$r" "$BRANCH_REQUIRED" && git push "$r" "v$NEW_VER" || {
    echo "  ✗ push to $r failed (tag is local/partial; re-run once fixed)" >&2; exit 1; }
  echo "  ✓ pushed to $r"
done

if [ "$DO_BINARY" = 1 ]; then
  missing="$(missing_installers)"
  if [ -n "$missing" ]; then
    echo "  installers still missing: $(echo $missing)"
    if [ -n "$GITHUB_PROJECT" ] && command -v gh >/dev/null 2>&1; then
      fetch_ci_installer "$(git rev-parse HEAD)" || exit 1
    fi
  fi
  missing="$(missing_installers)"
  if [ -n "$missing" ]; then
    echo "  ✗ a --binary release ships both platforms; still missing: $(echo $missing)" >&2
    echo "    build it on a Windows box and re-run with --installer <setup.exe>," >&2
    echo "    or configure the github remote and gh so CI can supply it" >&2
    exit 1
  fi
  # The fetched file is newer than the step-4 checksum list; recompute over the final set.
  ( cd dist && { sha256sum ./* bin/* 2>/dev/null | while read -r h f; do printf '%s  %s\n' "$h" "${f##*/}"; done; } > SHA256SUMS )
  echo "  ✓ both platform installers present, SHA256SUMS refreshed"
fi

if [ "$DO_RELEASE" = 0 ]; then
  echo "==> [6/6] release skipped (--no-release)"; exit 0
fi

publish_release "$NEW_VER"

# Verify what consumers actually get, not what the gates proved about the working tree.
if curl -sS -f -o /dev/null "$GITEE_API/repos/$GITEE_PROJECT/releases/tags/v$NEW_VER"; then
  echo "==> [7/7] consumer check"
  scripts/verify_release.sh "v$NEW_VER" || { echo "  ✗ published release is not consumable" >&2; exit 1; }
else
  echo "==> [7/7] consumer check skipped (no Release yet; after creating it run"
  echo "    scripts/verify_release.sh v$NEW_VER)"
fi
echo "==> done: v$NEW_VER"
