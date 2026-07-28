#!/usr/bin/env bash
#
# sync-candidate.sh — build and verify an upstream-sync candidate branch.
#
# Implements the fork's PR-based promotion model (adjudicated findings #4,
# #5, #12): detect the newest upstream release tag, replay the [local]
# patch stack onto it, and verify the result. This script NEVER pushes
# anything — the caller (the upstream-sync workflow) pushes the candidate
# branch and opens the PR; promotion to main is a human act via
# scripts/promote-sync.sh.
#
# SECURITY: verification executes upstream's freshly-fetched test code.
# Run this only in a context holding no fork-write credentials and no
# model keys (the unprivileged CI job, or a local shell).
#
# Usage:
#   sync-candidate.sh --output-dir DIR [--upstream URL] [--skip-tests]
#
# Exit code 0 with result=in-sync | candidate | blocked written to
# DIR/candidate.env (KEY=VALUE lines); nonzero only on infrastructure
# errors. On result=candidate, DIR also contains sync-candidate.bundle
# and candidate-report.md.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

UPSTREAM_URL="https://github.com/obra/superpowers.git"
OUTPUT_DIR=""
SKIP_TESTS=0
CANDIDATE_BRANCH="_sync-candidate"

# Deterministic upstream test subset for CI (LLM/behavioral tiers run
# locally at promote time — see promote-sync.sh). Each entry proven green
# on a clean environment before being listed here.
DETERMINISTIC_TESTS=(
  "tests/hooks/test-session-start.sh"
  "tests/shell-lint/test-lint-shell.sh"
  "tests/claude-code/test-sdd-workspace.sh"
  "tests/codex-plugin-sync/test-sync-to-codex-plugin.sh"
)

while [[ $# -gt 0 ]]; do
  case "$1" in
    --output-dir) OUTPUT_DIR="$2"; shift 2 ;;
    --upstream)   UPSTREAM_URL="$2"; shift 2 ;;
    --skip-tests) SKIP_TESTS=1; shift ;;
    *) echo "error: unknown argument '$1'" >&2; exit 2 ;;
  esac
done

if [[ -z "$OUTPUT_DIR" ]]; then
  echo "error: --output-dir is required" >&2
  exit 2
fi
mkdir -p "$OUTPUT_DIR"
ENV_FILE="$OUTPUT_DIR/candidate.env"
REPORT="$OUTPUT_DIR/candidate-report.md"

STACK_TIP="$(git rev-parse HEAD)"
ORIGINAL_BRANCH="$(git rev-parse --abbrev-ref HEAD)"

emit() {
  # KEY=VALUE lines consumed by the workflow via $GITHUB_OUTPUT.
  echo "$1=$2" >> "$ENV_FILE"
}

finish_blocked() {
  local reason="$1"
  echo ""
  echo "BLOCKED: $reason"
  emit result blocked
  emit reason "$reason"
  {
    echo "## Sync blocked"
    echo ""
    echo "**Reason:** $reason"
    echo ""
    echo "Stack tip at check time: \`$STACK_TIP\`"
  } > "$REPORT"
  exit 0
}

restore_worktree() {
  git cherry-pick --abort 2>/dev/null || true
  if [[ "$ORIGINAL_BRANCH" != "HEAD" ]]; then
    git checkout --quiet "$ORIGINAL_BRANCH" 2>/dev/null || true
  else
    git checkout --quiet "$STACK_TIP" 2>/dev/null || true
  fi
  git branch -D "$CANDIDATE_BRANCH" 2>/dev/null || true
}

# --- 1. Fetch upstream release tags into a collision-proof namespace ---
# (fork publication tags like vX.Y.Z-local.N share refs/tags/*; upstream
# tags live under refs/upstream-tags/* so neither side can clobber the
# other)
echo "Fetching upstream release tags from $UPSTREAM_URL..."
git fetch --no-tags --quiet --prune "$UPSTREAM_URL" '+refs/tags/v*:refs/upstream-tags/v*'

newest_tag=$(
  git for-each-ref --format='%(refname:short)' refs/upstream-tags/ \
    | sed 's|^upstream-tags/||' \
    | { grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' || true; } \
    | sort -V | tail -1
)
[[ -n "$newest_tag" ]] || { echo "error: no upstream release tags found" >&2; exit 1; }
newest_sha=$(git rev-parse "refs/upstream-tags/$newest_tag^{commit}")
echo "Newest upstream release: $newest_tag ($newest_sha)"

# Current base = newest upstream release tag that is an ancestor of HEAD.
current_base_tag=""
while IFS= read -r tag; do
  if git merge-base --is-ancestor "refs/upstream-tags/$tag" HEAD; then
    current_base_tag="$tag"
  fi
done < <(
  git for-each-ref --format='%(refname:short)' refs/upstream-tags/ \
    | sed 's|^upstream-tags/||' \
    | { grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' || true; } \
    | sort -V
)
[[ -n "$current_base_tag" ]] || { echo "error: no upstream tag is an ancestor of HEAD" >&2; exit 1; }
current_base_sha=$(git rev-parse "refs/upstream-tags/$current_base_tag^{commit}")
echo "Current fork base: $current_base_tag ($current_base_sha)"
emit tag "$newest_tag"
emit base "$current_base_tag"

# --- 2. Desired-state check (self-healing trigger, finding #12) ---
if [[ "$newest_sha" == "$current_base_sha" ]]; then
  echo "Fork main is already based on $newest_tag — nothing to sync."
  emit result in-sync
  emit reason "already based on $newest_tag"
  exit 0
fi

# --- 3. Classify the deviation list (iron rule: git log base..main IS
#        the [local] stack; anything unclassifiable fails closed) ---
stack_commits=()
while IFS=$'\t' read -r sha subject; do
  if [[ "$subject" =~ ^[a-z]+\(local\):\  ]]; then
    stack_commits+=("$sha")
  elif [[ "$subject" =~ ^chore\(release\):\  ]]; then
    # Publication commits never ride a sync — each promotion cuts a
    # fresh one (version bump would be stale on the new base).
    echo "Dropping publication commit from replay: $subject"
  else
    finish_blocked "unknown commit in deviation list (neither (local) patch nor (release) publication): $sha $subject"
  fi
done < <(git log --reverse --format='%H%x09%s' "$current_base_sha..HEAD")

[[ ${#stack_commits[@]} -gt 0 ]] || finish_blocked "deviation list contains no [local] patch commits — nothing to replay (unexpected state)"
echo "Stack to replay: ${#stack_commits[@]} commit(s)"

# --- 4. Structural tripwire (finding #4): upstream touched a patched
#        skill dir -> never auto-promote ---
patched_skill_dirs=$(
  for sha in "${stack_commits[@]}"; do
    git diff-tree --no-commit-id --name-only -r "$sha"
  done | { grep -E '^skills/[^/]+/' || true; } | cut -d/ -f1-2 | sort -u
)
tripwire_hits=""
if [[ -n "$patched_skill_dirs" ]]; then
  # shellcheck disable=SC2086 # dir list is newline-split on purpose
  tripwire_hits=$(git diff --name-only "$current_base_sha".."$newest_sha" -- $patched_skill_dirs || true)
fi
if [[ -n "$tripwire_hits" ]]; then
  finish_blocked "tripwire: upstream $current_base_tag..$newest_tag touches patched skill dir(s): $(echo "$tripwire_hits" | cut -d/ -f1-2 | sort -u | tr '\n' ' ')"
fi
echo "Tripwire clean: upstream did not touch any patched skill dir."

# --- 5. Replay the stack onto the new tag ---
# Cherry-pick commits carry their original messages through the local
# commit-msg hook; the progress-freshness pre-commit hook is disabled for
# the replay (replayed commits are not new work — its own escape hatch,
# same rationale as adjudicated finding #8).
export CODEX_PROGRESS_HOOK_ENABLED=false
git config user.name  >/dev/null 2>&1 || git config user.name  "upstream-sync"
git config user.email >/dev/null 2>&1 || git config user.email "upstream-sync@localhost"

trap restore_worktree ERR
git checkout --quiet -B "$CANDIDATE_BRANCH" "$newest_sha"
for sha in "${stack_commits[@]}"; do
  subject=$(git log -1 --format='%s' "$sha")
  if ! git cherry-pick "$sha" >/dev/null 2>&1; then
    restore_worktree
    trap - ERR
    finish_blocked "conflict (or empty result) replaying [local] commit: $sha $subject"
  fi
  echo "  replayed: $subject"
done
trap - ERR
candidate_sha=$(git rev-parse HEAD)
emit candidate_sha "$candidate_sha"
echo "Candidate built: $candidate_sha"

# --- 6. Intent checks on the candidate tree (finding #4: anchored +
#        negative assertions; textual rebase success != semantic survival) ---
intent_results=""
for check in scripts/intent-checks/*.sh; do
  if bash "$check" >/dev/null 2>&1; then
    intent_results+="- PASS: $check"$'\n'
  else
    restore_worktree
    finish_blocked "intent check failed on candidate: $check"
  fi
done
echo "Intent checks: all PASS"

# --- 7. Deterministic upstream test subset ---
test_results=""
if [[ "$SKIP_TESTS" -eq 1 ]]; then
  test_results="- SKIPPED (--skip-tests)"$'\n'
  echo "Upstream tests: SKIPPED"
else
  for t in "${DETERMINISTIC_TESTS[@]}"; do
    if [[ ! -f "$t" ]]; then
      test_results+="- MISSING (upstream moved it?): $t"$'\n'
      continue
    fi
    if bash "$t" >"$OUTPUT_DIR/$(basename "$t").log" 2>&1; then
      test_results+="- PASS: $t"$'\n'
    else
      restore_worktree
      finish_blocked "upstream deterministic test failed on candidate: $t"
    fi
  done
  echo "Upstream deterministic tests: all PASS"
fi

# --- 8. Bundle + report ---
git bundle create "$OUTPUT_DIR/sync-candidate.bundle" "$CANDIDATE_BRANCH" >/dev/null 2>&1
{
  echo "## Upstream sync candidate: $newest_tag"
  echo ""
  echo "| | |"
  echo "|---|---|"
  echo "| Upstream tag | \`$newest_tag\` (\`$newest_sha\`) |"
  echo "| Previous base | \`$current_base_tag\` |"
  echo "| Candidate head | \`$candidate_sha\` |"
  echo ""
  echo "### Replayed [local] stack"
  echo ""
  git log --reverse --format='- `%h` %s' "$newest_sha".."$candidate_sha"
  echo ""
  echo "### Verification"
  echo ""
  echo "Tripwire (upstream vs patched skill dirs): clean"
  echo ""
  echo "$intent_results"
  echo "$test_results"
  echo "LLM/behavioral test tiers are NOT run in CI — run them locally at"
  echo "promote time (see scripts/promote-sync.sh)."
  echo ""
  echo "### Promotion"
  echo ""
  echo "**Do NOT use the merge button** — every GitHub merge mode corrupts"
  echo "the linear patch-stack history (main is *replaced* by the candidate,"
  echo "not merged with it). Promote from the maintainer clone:"
  echo ""
  echo '```'
  echo "scripts/promote-sync.sh $newest_tag"
  echo '```'
} > "$REPORT"

restore_worktree
emit result candidate
emit reason "candidate ready for $newest_tag"
echo ""
echo "Candidate ready: bundle + report written to $OUTPUT_DIR"
