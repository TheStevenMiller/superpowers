#!/usr/bin/env bash
#
# promote-sync.sh — promote a verified sync candidate to main and cut the
# fork release, atomically from the maintainer's point of view.
#
# This is the human half of PR-based promotion (RESEARCH.md §4, findings
# #5 + #12): the upstream-sync workflow builds sync/<tag> and opens a PR;
# a human reviews it and runs THIS script. The GitHub merge button is
# never used — no merge mode can express "replace main with the
# candidate", which is what the linear patch-stack model requires. When
# main moves to the candidate head, GitHub marks the PR merged on its own
# (head becomes reachable from base).
#
# Usage:
#   promote-sync.sh <upstream-tag> [<fork-version>] [--with-behavioral]
#
#   <upstream-tag>     e.g. v6.3.0 — the candidate branch is sync/<tag>
#   <fork-version>     publication version for the release cut
#                      (default: X.Y.(Z+1)-local.1 derived from the tag)
#   --with-behavioral  also run the LLM/behavioral test tier locally
#                      (tests/claude-code/run-skill-tests.sh — slow)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

TAG="${1:-}"
FORK_VERSION="${2:-}"
WITH_BEHAVIORAL=0
for arg in "$@"; do
  [[ "$arg" == "--with-behavioral" ]] && WITH_BEHAVIORAL=1
done
if [[ "$FORK_VERSION" == "--with-behavioral" ]]; then FORK_VERSION=""; fi

if [[ -z "$TAG" ]] || ! [[ "$TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "usage: promote-sync.sh <upstream-tag> [<fork-version>] [--with-behavioral]" >&2
  exit 2
fi

if [[ -z "$FORK_VERSION" ]]; then
  # vX.Y.Z -> X.Y.(Z+1)-local.1 : sorts above the upstream base in semver,
  # can never collide with an upstream tag (upstream never cuts -local.*)
  IFS=. read -r major minor patch <<< "${TAG#v}"
  FORK_VERSION="$major.$minor.$((patch + 1))-local.1"
fi

AGENT_TRAILER="${AGENT_TRAILER:-Agent: promote-sync (script)}"
CANDIDATE_REF="sync/$TAG"

# --- Preflight (fail closed) ---
[[ -f .claude-plugin/plugin.json ]] || { echo "error: run from the fork repo root" >&2; exit 1; }
[[ -z "$(git status --porcelain)" ]] || { echo "error: worktree not clean" >&2; exit 1; }
command -v gh >/dev/null || { echo "error: gh CLI required" >&2; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "error: gh not authenticated" >&2; exit 1; }

# The maintainer clone has two remotes (origin=fork, upstream), which
# breaks gh's repo auto-resolution — pin every gh call to the fork.
ORIGIN_SLUG=$(git remote get-url origin | sed -E 's#^(git@github\.com:|https://github\.com/)##; s#\.git$##')
[[ "$ORIGIN_SLUG" == */* ]] || { echo "error: cannot derive owner/repo from origin URL" >&2; exit 1; }
export GH_REPO="$ORIGIN_SLUG"

echo "Fetching origin..."
git fetch origin

pr_number=$(gh pr list --head "$CANDIDATE_REF" --base main --state open \
  --json number --jq '.[0].number // empty')
[[ -n "$pr_number" ]] || { echo "error: no open PR with head $CANDIDATE_REF — run the upstream-sync workflow first" >&2; exit 1; }
pr_head=$(gh pr view "$pr_number" --json headRefOid --jq '.headRefOid')

git fetch origin "+refs/heads/$CANDIDATE_REF:refs/remotes/origin/$CANDIDATE_REF"
candidate_sha=$(git rev-parse "refs/remotes/origin/$CANDIDATE_REF")
[[ "$candidate_sha" == "$pr_head" ]] || { echo "error: PR head ($pr_head) != fetched candidate ($candidate_sha) — stale PR?" >&2; exit 1; }

# --- Local re-verification of the candidate (the promotion gate is
#     local: the sync workflow's checks ran pre-push, not as PR checks) ---
echo "Verifying candidate $candidate_sha locally..."
original_ref=$(git rev-parse HEAD)
cleanup() { git checkout --quiet "$original_ref" 2>/dev/null || true; git branch -D _promote-verify 2>/dev/null || true; }
trap cleanup EXIT
git checkout --quiet -B _promote-verify "$candidate_sha"

for check in scripts/intent-checks/*.sh; do
  bash "$check" >/dev/null || { echo "error: intent check failed on candidate: $check" >&2; exit 1; }
done
echo "  intent checks: PASS"

for t in tests/hooks/test-session-start.sh tests/shell-lint/test-lint-shell.sh \
         tests/claude-code/test-sdd-workspace.sh tests/codex-plugin-sync/test-sync-to-codex-plugin.sh; do
  [[ -f "$t" ]] || continue
  GIT_CONFIG_GLOBAL=/dev/null bash "$t" >/dev/null 2>&1 || { echo "error: deterministic test failed on candidate: $t" >&2; exit 1; }
done
echo "  deterministic tests: PASS"

if [[ "$WITH_BEHAVIORAL" -eq 1 ]]; then
  echo "  running behavioral tier (slow)..."
  bash tests/claude-code/run-skill-tests.sh || { echo "error: behavioral tests failed" >&2; exit 1; }
else
  echo "  behavioral tier: skipped (pass --with-behavioral to run it)"
fi

echo ""
echo "Deviation list after promotion ($TAG..candidate):"
git log --reverse --format='  %h %s' "refs/upstream-tags/$TAG..$candidate_sha" 2>/dev/null \
  || git log --reverse --format='  %h %s' "$TAG..$candidate_sha"
echo ""
echo "This will REPLACE origin/main with the candidate and cut release v$FORK_VERSION."
printf 'Type PROMOTE to continue: '
read -r confirmation
[[ "$confirmation" == "PROMOTE" ]] || { echo "aborted"; exit 1; }

# --- Promote: move main to the candidate (lease-protected) ---
main_before=$(git rev-parse refs/remotes/origin/main)
git push --force-with-lease="refs/heads/main:$main_before" origin "$candidate_sha:refs/heads/main"
echo "origin/main -> $candidate_sha (PR #$pr_number will show as merged)"

trap - EXIT
git checkout --quiet -B main "$candidate_sha"
git branch --set-upstream-to=origin/main main >/dev/null
git branch -D _promote-verify 2>/dev/null || true

# --- Release cut: publication commit + tag OUTSIDE the patch stack ---
scripts/bump-version.sh "$FORK_VERSION"
git add -A
git commit -m "chore(release): cut v$FORK_VERSION — fork publication on $TAG base

Publication commit outside the [local] patch stack: version bump across
the declared manifests so plugin version-resolution delivers this release
(a matching resolved version makes plugin update a no-op).

$AGENT_TRAILER"
git push origin main

# Release tag is created SERVER-SIDE via the API, not git-pushed: a new-tag
# push has no remote base, so the machine-global pre-push hook falls back to
# linting the entire tree (including upstream's .py). The tag adds no
# content — it points at the commit main just shipped, which went through
# the hooks. (Policy decision 2026-07-27.)
release_commit=$(git rev-parse HEAD)
tag_object=$(gh api "repos/$ORIGIN_SLUG/git/tags" \
  -f tag="v$FORK_VERSION" \
  -f message="Fork release v$FORK_VERSION on $TAG base" \
  -f object="$release_commit" -f type=commit --jq .sha)
[[ -n "$tag_object" ]] || { echo "error: server-side tag object creation failed" >&2; exit 1; }
gh api "repos/$ORIGIN_SLUG/git/refs" \
  -f ref="refs/tags/v$FORK_VERSION" -f sha="$tag_object" >/dev/null
git fetch --no-tags origin "+refs/tags/v$FORK_VERSION:refs/tags/v$FORK_VERSION"
echo "release tag v$FORK_VERSION created server-side and fetched back"

echo ""
echo "Promoted + released v$FORK_VERSION."
echo "Local delivery: scripts/update-hop.sh (or wait for the daily launchd run)."
