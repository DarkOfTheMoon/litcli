#!/usr/bin/env bash
# =============================================================================
# release.sh - litcli semi-automatic release (gated, non-interactive)
#
# Flow, mirroring qbtt's release.sh but for a pip package hosted on Gitee:
#   1) gates (any failure -> exit 1, nothing is written)
#      ① on master and level with origin        ② worktree clean
#      ③ full test suite green                  ④ ruff clean
#      ⑤ package builds (sdist + wheel)         ⑥ CHANGELOG [Unreleased] non-empty
#      ⑦ target version not tagged yet          ⑧ target is plain X.Y.Z and, after the bump, the
#                                                  written __version__ equals it
#   2) bump `litcli/_version.py` (--bump major|minor|patch, default patch; `none` releases the
#      current __version__ in place) and harvest CHANGELOG [Unreleased] into a dated section
#   3) commit `chore(release): bump to X.Y.Z, harvest changelog` + tag vX.Y.Z
#      (`--bump none` uses `cut release vX.Y.Z, harvest changelog` instead)
#   4) build dist/ artifacts + SHA256SUMS ([--binary] also builds this platform's installer)
#   5) only with --push: push master + tag to origin (and to `github` when that remote
#      exists), and create the Gitee Release with the artifacts attached when a token is
#      configured (otherwise print the manual steps)
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
#                                                               bumped, committed or pushed. Requires
#                                                               HEAD == the tag on a clean tree, rebuilds
#                                                               dist/ and runs the consumer check.
#                                                               Takes --binary (and --installer) the
#                                                               same way the full path does; this is
#                                                               how a package-only release is later
#                                                               given its installers.
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

write_sums() {
  # `sha256sum ./* bin/*` would also be handed the dist/bin directory, which sha256sum
  # rejects — harmless without pipefail, fatal with it. Enumerate files instead.
  # Normalise to `hash  name`, flat: attachments download side by side, so a `bin/` prefix
  # would not resolve. Also strips the `./` and Git Bash's binary-mode `*`.
  ( cd dist && find . -maxdepth 2 -type f ! -name SHA256SUMS -print0 \
    | sort -z | xargs -0 sha256sum 2>/dev/null \
    | sed -E 's# ([ *])\./(bin/)?#  #' > SHA256SUMS )
}

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
    id="$(gh run list --repo "$GITHUB_PROJECT" --commit "$sha" --event push --limit 1 \
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

  local payload resp id f failed=0 attached="" auth
  payload="$(mktemp)"; resp="$(mktemp)"; auth="$(mktemp)"; listing="$(mktemp)"
  # The token goes into a curl config file (mode 600) rather than a -H argument, so it is not
  # readable from `ps` for the lifetime of every upload.
  chmod 600 "$auth"
  printf 'header = "Authorization: token %s"\n' "$GITEE_TOKEN" > "$auth"
  trap 'rm -f "$payload" "$resp" "$auth" "$listing"' RETURN
  # A retry after a half-failed upload must attach to the existing Release, not POST a duplicate
  # (Gitee rejects a second Release for the same tag). An asset of the same name is replaced
  # rather than uploaded twice (a retry must not end up as `name (1).whl`).
  if curl -sS -f --connect-timeout 10 --max-time 60 --config "$auth" "$GITEE_API/repos/$GITEE_PROJECT/releases/tags/v$ver" -o "$resp" 2>/dev/null; then
    # A tag without a Release answers `200` with a bare `null` (not a 404), so the body decides.
    id="$(python3 -c "import json,sys; print((json.load(open(sys.argv[1])) or {}).get('id',''))" "$resp")"
    if [ -n "$id" ]; then
      echo "  - Release v$ver already exists (id $id), attaching to it"
      # The release object's own ``assets`` carry no ids; the attach listing does. Without the
      # ids a same-named asset could only be added a second time, not replaced.
      if curl -sS -f --connect-timeout 10 --max-time 60 --config "$auth" \
          "$GITEE_API/repos/$GITEE_PROJECT/releases/$id/attach_files" -o "$listing" 2>/dev/null; then
        attached="$(python3 -c "import json,sys; print('\n'.join(f\"{a['id']}\t{a['name']}\" for a in (json.load(open(sys.argv[1])) or [])))" "$listing")"
      fi
    else
      echo "  - no Release for v$ver yet (Gitee answers a missing one with an empty 200)"
    fi
  fi
  if [ -z "$id" ]; then
    NOTES="$notes" python3 - "$payload" "$ver" <<'PAYLOAD'
import json, os, sys
json.dump({"tag_name": f"v{sys.argv[2]}", "name": f"litcli v{sys.argv[2]}",
           "body": os.environ["NOTES"], "target_commitish": "master", "prerelease": False},
          open(sys.argv[1], "w", encoding="utf-8"), ensure_ascii=False)
PAYLOAD
    curl -sS -f --connect-timeout 10 --max-time 60 -X POST "$GITEE_API/repos/$GITEE_PROJECT/releases" \
      --config "$auth" -H "Content-Type: application/json" \
      --data-binary "@$payload" > "$resp" || {
        echo "  ✗ Release creation failed (is the tag pushed? does the token have the release scope?)" >&2
        return 1; }
    id="$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['id'])" "$resp")"
    echo "  ✓ Release v$ver created (id $id)"
  fi
  # A Release that silently misses an asset is worse than a failed run: the checksum list
  # published with it would not resolve.
  for f in dist/* dist/bin/*; do
    [ -f "$f" ] || continue
    # Replace an existing asset of the same name: this run rebuilt the artifacts (and
    # SHA256SUMS), so the old bytes would no longer match the published checksums.
    old_id="$(awk -F'\t' -v n="$(basename "$f")" '$2 == n { print $1 }' <<<"$attached")"
    if [ -n "$old_id" ]; then
      curl -sS -f --connect-timeout 10 --max-time 60 -X DELETE \
        "$GITEE_API/repos/$GITEE_PROJECT/releases/$id/attach_files/$old_id" --config "$auth" >/dev/null \
        || { echo "  ✗ could not replace $(basename "$f")" >&2; failed=1; continue; }
    fi
    if curl -sS -f --connect-timeout 10 --max-time 1800 -X POST "$GITEE_API/repos/$GITEE_PROJECT/releases/$id/attach_files" \
        --config "$auth" -F "file=@$f" >/dev/null; then
      echo "  ✓ attached $(basename "$f")"
    else
      echo "  ✗ attach failed: $(basename "$f")" >&2; failed=1
    fi
  done
  return "$failed"
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
    # Everything from line 2 to the first non-comment line: the header grows without the
    # help text silently losing its tail.
    -h|--help) awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done
case "$BUMP" in major|minor|patch|none) ;; *) echo "--bump must be major|minor|patch|none" >&2; exit 2 ;; esac

# The gates and the artifact build need tools that are not runtime dependencies. Fail here with
# the install line, not halfway through with "command not found".
DEV_HINT="python3 -m pip install -e '.[dev]'"
need() {  # need <command> <install hint>
  command -v "$1" >/dev/null 2>&1 && return 0
  echo "  ✗ $1 is required; $2" >&2
  exit 1
}
echo "==> preflight"
need ruff "$DEV_HINT"
python3 -c 'import build' >/dev/null 2>&1 \
  || { echo "  ✗ the 'build' module is required; $DEV_HINT" >&2; exit 1; }
if [ "$DO_BINARY" = 1 ] && [ -n "$GITHUB_PROJECT" ]; then
  need gh "install the GitHub CLI (https://cli.github.com) so CI can supply the other installer"
fi
echo "  ✓ ruff, build${GITHUB_PROJECT:+$( [ "$DO_BINARY" = 1 ] && echo ', gh')}"

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
  # The same two gates the full path relies on: artifacts must come from the tagged commit's
  # tree, and a dirty tree means whatever sits in dist/ may be from anything.
  [ "$(git rev-parse HEAD)" = "$(git rev-parse "v$REL_VER^{commit}")" ] || {
    echo "  ✗ HEAD is not v$REL_VER; check it out first (artifacts are built from the worktree)" >&2
    exit 1; }
  [ -z "$(git status --porcelain --untracked-files=normal)" ] || {
    echo "  ✗ worktree has changes" >&2; exit 1; }
  # Always rebuild: a dist/ left over from an earlier run can hold another version's files
  # (attach_files would ship them).
  keep="$(mktemp -d)"
  if [ -d dist/bin ]; then cp -a dist/bin "$keep/bin"; fi
  rm -rf dist
  if ! python3 -m build --outdir dist >/dev/null; then
    if [ -d "$keep/bin" ]; then mkdir -p dist/bin && cp -a "$keep/bin/." dist/bin/; fi
    rm -rf "$keep"; echo "  ✗ rebuild failed" >&2; exit 1
  fi
  if [ -d "$keep/bin" ]; then mkdir -p dist/bin && cp -a "$keep/bin/." dist/bin/; fi
  rm -rf "$keep"
  echo "  ✓ dist/ rebuilt"
  # Same binary step as the full path: a re-release of a package-only release is exactly where
  # --binary is used, so it must not depend on dist/bin already having something in it.
  if [ "$DO_BINARY" = 1 ]; then
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
  # A Release that lists installers must list both platforms; top up from CI before publishing.
  if [ "$DO_BINARY" = 1 ] || ls dist/bin/*-* >/dev/null 2>&1; then
    if [ -n "$(missing_installers)" ] && [ -n "$GITHUB_PROJECT" ] && command -v gh >/dev/null 2>&1; then
      fetch_ci_installer "$(git rev-parse "v$REL_VER^{commit}")" || exit 1
    fi
    [ -z "$(missing_installers)" ] || {
      echo "  ✗ release has installers but misses: $(missing_installers | tr '\n' ' ')" >&2; exit 1; }
  fi
  write_sums
  publish_release "$REL_VER" || exit 1
  # Same consumer check as the full path: what was just attached must be installable.
  echo "==> consumer check"
  scripts/verify_release.sh "v$REL_VER" || { echo "  ✗ published release is not consumable" >&2; exit 1; }
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

# A gate's detail is what lets the next run be fixed; discarding it leaves only "failed".
gate_cmd() {  # gate_cmd <label> <command...>
  local label="$1"; shift
  local log; log="$(mktemp)"
  if "$@" >"$log" 2>&1; then echo "  ✓ $label"; rm -f "$log"; return 0; fi
  echo "  ✗ $label failed (last 25 lines):" >&2; tail -n 25 "$log" >&2; rm -f "$log"; return 1
}

echo "  - branch and sync state ..."
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if [ "$BRANCH" = "$BRANCH_REQUIRED" ]; then echo "  ✓ on $BRANCH"; else
  echo "  ✗ on $BRANCH, releases are cut from $BRANCH" >&2; fail="$fail,branch"; fi
# Without a fetch the comparison runs against a stale origin/<branch> and can pass a dozing tree.
git fetch --quiet origin "$BRANCH_REQUIRED" 2>/dev/null \
  || echo "  ! git fetch failed (offline?); comparing against the last known origin" >&2
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
gate_cmd "tests green" python3 -m unittest discover -s tests || fail="$fail,tests"

echo "  - lint (ruff) ..."
gate_cmd "lint clean" ruff check litcli tests || fail="$fail,lint"

# A build failure must surface here, not after the version has been bumped and committed.
# An isolated build (the default) compiles in a fresh environment, the way a consumer's wheel
# ends up built, instead of trusting whatever happens to be importable right now.
echo "  - package builds ..."
BUILD_DIR="$(mktemp -d)"
gate_cmd "sdist + wheel build" python3 -m build --outdir "$BUILD_DIR" || fail="$fail,build"
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
case "$NEW_VER" in
  [0-9]*.[0-9]*.[0-9]*) echo "  ✓ target v$NEW_VER is a plain X.Y.Z" ;;
  *) echo "  ✗ target v$NEW_VER is not X.Y.Z" >&2; fail="$fail,version" ;;
esac
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
  # Gate ⑧, on the written file: what got tagged must be the version that was gated.
  [ "$(python3 -c "import re,pathlib; print(re.search(r'__version__ = \"(.*)\"', pathlib.Path('$VERSION_FILE').read_text()).group(1))")" = "$NEW_VER" ] \
    || { echo "  ✗ $VERSION_FILE does not carry $NEW_VER after the bump" >&2; exit 1; }
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
python3 -m build --outdir dist >/dev/null
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
write_sums
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
# origin always; `github` too when it exists, because the mirror runs CI and must not lag
# behind what was released. Any other remote is not pushed (name it in the list below to add it).
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
  write_sums
  echo "  ✓ both platform installers present, SHA256SUMS refreshed"
fi

if [ "$DO_RELEASE" = 0 ]; then
  echo "==> [6/6] release skipped (--no-release)"; exit 0
fi

publish_release "$NEW_VER"

# Verify what consumers actually get, not what the gates proved about the working tree.
if curl -sS -f --connect-timeout 10 --max-time 60 -o /dev/null \
     "$GITEE_API/repos/$GITEE_PROJECT/releases/tags/v$NEW_VER"; then
  echo "==> [7/7] consumer check"
  scripts/verify_release.sh "v$NEW_VER" || { echo "  ✗ published release is not consumable" >&2; exit 1; }
else
  echo "==> [7/7] consumer check skipped (no Release yet; after creating it run"
  echo "    scripts/verify_release.sh v$NEW_VER)"
fi
echo "==> done: v$NEW_VER"
