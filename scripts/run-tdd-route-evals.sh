#!/usr/bin/env bash
# Manual behavioral tier for [local] patch #5 (A16/A22(4)): run the four
# TDD-lane route evals against the INSTALLED plugin in real headless Claude
# Code sessions. This is a documented MANUAL pre-release step — run it
# before cutting any release that includes patch 5; it is deliberately not
# wired into sync-candidate's --with-behavioral (patch-4-owned list).
#
# Each scenario drives claude -p in a scratch repo and greps the transcript
# for its PASS/FAIL markers, then prints a human-judged summary table —
# read the transcripts on any ambiguity; the grep is a hint, not a judge.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCENARIOS="$SCRIPT_DIR/../tests/claude-code/tdd-route-evals"
command -v claude >/dev/null 2>&1 || { echo "claude CLI not found" >&2; exit 1; }

WORK=$(mktemp -d "${TMPDIR:-/tmp}/tdd-route-evals.XXXXXX")
trap 'rm -rf "$WORK"' EXIT

mk_scratch_repo() { # mk_scratch_repo DIR — committed failing test, clean tree
  mkdir -p "$1/tests"
  git -C "$1" init -q
  git -C "$1" checkout -q -b main 2>/dev/null || true
  git -C "$1" config user.name eval
  git -C "$1" config user.email eval@example.invalid
  printf 'def test_feature():\n    from feature import feature\n    assert feature() == 1\n' >"$1/tests/test_feature.py"
  git -C "$1" add -A
  git -C "$1" commit -qm 'test: failing feature test

Agent: eval-fixture (setup)'
}

run_scenario() { # run_scenario N PROMPT GREP_PASS GREP_FAIL
  local n=$1 prompt=$2 want=$3 ban=$4 repo="$WORK/repo-$1" out="$WORK/out-$1"
  mk_scratch_repo "$repo"
  if [ "$n" = "3" ]; then
    local common ws
    common=$(git -C "$repo" rev-parse --path-format=absolute --git-common-dir)
    ws="$HOME/.claude/tdd-lane/$(printf '%s' "$common" | shasum -a 256 | awk '{print substr($1,1,16)}')/main"
    mkdir -p "$ws"
    printf 'repo=%s\nbranch=main\nphase=escalated\nbase_sha=%s\nfix_rounds_used=3\nescalated=true\n' \
      "$common" "$(git -C "$repo" rev-parse HEAD)" >"$ws/state"
  fi
  printf '\n=== scenario %s ===\n' "$n"
  ( cd "$repo" && timeout 300 claude -p "$prompt" \
      --allowed-tools 'Read,Grep,Glob,Bash' ) >"$out" 2>&1 || true
  if grep -qiE "$want" "$out" && ! grep -qiE "$ban" "$out"; then
    printf 'scenario %s: PASS (heuristic — read %s to confirm)\n' "$n" "$out"
  else
    printf 'scenario %s: REVIEW NEEDED — read %s\n' "$n" "$out"
  fi
}

echo "Route evals (behavioral tier). Scenario specs: $SCENARIOS"
run_scenario 1 \
  "Implement the feature that makes the committed failing test in tests/ green. Use test-driven development." \
  'codex-lane|tdd-codex-dispatch' \
  '__never_matches__'
run_scenario 3 \
  "Implement the feature that makes the committed failing test in tests/ green. Use test-driven development." \
  'escalat' \
  '__never_matches__'
echo "scenario 2 (subagent) and scenario 4 (unrelated cwd) need interactive/context setup:"
echo "  follow $SCENARIOS/scenario-2-subagent-no-route.md and scenario-4-unrelated-cwd.md manually."
echo "Record PASS/FAIL per scenario in the release notes before cutting."
