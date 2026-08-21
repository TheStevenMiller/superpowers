#!/usr/bin/env bash
# LIVE e2e smoke for the TDD lane (§9.3) — real codex, ChatGPT billing lane.
# NOT part of the fixture suite or any intent check: costs real tokens and
# minutes. Run manually during the build wave and before releases when the
# adapter changed. The contested / protected-change / fix-round smokes are
# controller-driven (see the checklist this script prints at the end).
set -euo pipefail

command -v codex >/dev/null 2>&1 || { echo "codex CLI required" >&2; exit 1; }

WORK=$(mktemp -d "${TMPDIR:-/tmp}/tdd-e2e.XXXXXX")
echo "workdir: $WORK (kept on failure for inspection)"
REPO="$WORK/repo"
mkdir -p "$REPO/tests"
git -C "$REPO" init -q
git -C "$REPO" checkout -q -b main 2>/dev/null || true
git -C "$REPO" config user.name smoke
git -C "$REPO" config user.email smoke@example.invalid
cat >"$REPO/tests/test_slug.py" <<'EOF'
from slug import slugify

def test_basic():
    assert slugify("Hello World") == "hello-world"

def test_strips_punctuation():
    assert slugify("Rock & Roll!") == "rock-roll"

def test_collapses_spaces():
    assert slugify("a   b") == "a-b"
EOF
# pytest's cacheprovider auto-creates a self-ignoring .pytest_cache/.gitignore,
# which the adapter's veiled scan flags by design — the brief's test command
# disables it; the fixture .gitignore keeps bytecode out of the protected scans.
printf '__pycache__/\n*.pyc\n' >"$REPO/.gitignore"
git -C "$REPO" -c core.hooksPath=/dev/null add -A
# Fixture trailer: a literal stand-in for the session model a real lane run
# fills in (the adapter never parses the test-author trailer; the commit-msg
# hook accepts any `Agent: <role> (<model>)`).
git -C "$REPO" -c core.hooksPath=/dev/null commit -qm 'test: slugify spec

Tests-authored-by: Claude (smoke fixture)
Agent: test-author (smoke fixture)'
BASE=$(git -C "$REPO" rev-parse HEAD)

ADAPTER="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/scripts/tdd-codex-dispatch"
common=$(git -C "$REPO" rev-parse --path-format=absolute --git-common-dir)
WS="$HOME/.claude/tdd-lane/$(printf '%s' "$common" | shasum -a 256 | awk '{print substr($1,1,16)}')/main"
mkdir -p "$WS"
printf 'repo=%s\nbranch=main\nphase=red-committed\nbase_sha=%s\nfix_rounds_used=0\nescalated=false\n' \
  "$common" "$BASE" >"$WS/state"

rc=0
printf '%s' "# TDD Lane Dispatch — slugify

Task: implement slugify(text) in slug.py at the repo root so the committed
failing tests pass.

The committed failing tests are THE spec:
- $REPO/tests/test_slug.py
Run them with: python3 -m pytest -p no:cacheprovider tests/ -q

Implementation discipline (this brief is the complete contract — it
overrides any general TDD methodology you know):
- Write the simplest code that makes the given tests green.
- Add no behavior beyond what the tests specify.
- Refactor only while staying green.
- Respect the codebase's existing patterns and seams.

Rules:
- Tests and all test-adjacent files are immutable; no new files there.
- No hardcoding keyed to test inputs; general-purpose solutions.
- Work in the worktree only. Do NOT commit, do NOT run git write operations.
- Remove scratch files; leave the tree ready to commit.
- Any exit other than DONE must leave the repository exactly as dispatched.

Statuses: DONE | DONE_WITH_CONCERNS | BLOCKED | NEEDS_CONTEXT |
TEST_CONTESTED | PROTECTED_CHANGE_REQUESTED (specificity rules apply).

End your final message with exactly one report block:
TDD-DISPATCH-RESULT
status: <STATUS>
<what you implemented>
<test command + observed output>
" | "$ADAPTER" \
  --worktree "$REPO" --branch main \
  --commit-subject 'feat: implement slugify' \
  --effort medium --timeout-seconds 1200 || rc=$?

echo "adapter exit: $rc"
git -C "$REPO" log --oneline -3
git -C "$REPO" show -s --format=%B HEAD
if [ "$rc" -eq 0 ]; then rm -rf "$WORK"; fi

cat <<'CHECKLIST'
--- Controller-driven smokes still to run (record evidence in the report) ---
[ ] contested branch: dispatch with a deliberately wrong test expectation;
    expect TEST_CONTESTED with a specific claim; uphold -> fix test on base
    lineage, re-freeze, fresh dispatch -> DONE (§9.3)
[ ] protected-change branch: task needing a new dependency; expect
    PROTECTED_CHANGE_REQUESTED naming package.json + concrete change;
    uphold -> apply on base lineage, re-freeze, dispatch -> DONE
[ ] one fix round: after a DONE with a failing disposable-checkout GREEN,
    re-dispatch --fix-round 1 with raw output payload; expect amend (SHA
    changed, sole parent == base)
CHECKLIST
