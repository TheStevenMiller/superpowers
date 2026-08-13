#!/usr/bin/env bash
# Intent checks for [local] patch #2 — SDD Sol implementer/fix lanes.
#
# Run from the repo root; exit 0 means the patch's operative surface is
# intact. Checks follow the anchored form (operative-line anchor + negative
# assertion that the replaced mechanism is gone): a textual rebase that
# "applies" but strands the patch semantically must fail here. If any check
# fails after an upstream sync, do not promote — open the sync-blocked issue.
# Negative assertions use the fail-closed form "grep … && exit 1" — a bare
# "! grep" is exempt from set -e and asserts nothing.
set -euo pipefail

sdd=skills/subagent-driven-development

# The dispatch adapter exists, is executable, and keeps its operative
# guarantees: direct codex exec dispatch, pinned Sol model, git dirs granted
# back into the workspace-write sandbox, ChatGPT billing-lane assertion,
# exact implementer trailer, fresh-only dispatch (no session resume)
test -x scripts/sdd-codex-dispatch
grep -q 'codex exec' scripts/sdd-codex-dispatch
grep -q 'gpt-5.6-sol' scripts/sdd-codex-dispatch
grep -q -- '--add-dir' scripts/sdd-codex-dispatch
grep -q 'workspace-write' scripts/sdd-codex-dispatch
grep -q 'auth_mode' scripts/sdd-codex-dispatch
grep -q 'Agent: implementer (GPT 5.6 Sol)' scripts/sdd-codex-dispatch
grep -q -- '--resume' scripts/sdd-codex-dispatch && exit 1
grep -q 'codex-companion' scripts/sdd-codex-dispatch && exit 1

# implementer-prompt.md: the adapter dispatch header is the operative line;
# the choose-at-dispatch Agent-tool header (MODEL placeholder) must be gone
grep -q 'sdd-codex-dispatch' "$sdd/implementer-prompt.md"
grep -q 'MODEL — REQUIRED' "$sdd/implementer-prompt.md" && exit 1
grep -q 'Subagent (general-purpose)' "$sdd/implementer-prompt.md" && exit 1

# Adapter resolution is plugin-root-derived (TDD-lane review finding #4 /
# ledger A7 spillover): the absolute-resolution anchor is present; the bare
# command-position invocation and the false "(repo root)" location are gone
grep -qF '<plugin-root>/scripts/sdd-codex-dispatch' "$sdd/implementer-prompt.md"
grep -qE '^[[:space:]]+scripts/sdd-codex-dispatch' "$sdd/implementer-prompt.md" && exit 1
grep -qF '(repo root)' "$sdd/implementer-prompt.md" && exit 1

# SKILL.md Model Selection: the Sol lane table is present and the Task Loop
# dispatch line is harness-conditional (non-Claude harnesses get native
# subagent dispatch, never a shell-out to a machine-local CLI lane)
grep -q 'gpt-5.6-sol' "$sdd/SKILL.md"
grep -q 'sdd-codex-dispatch' "$sdd/SKILL.md"
grep -q 'codex-tools.md' "$sdd/SKILL.md"

# Reviewer lanes are patch #1's files — this patch must never touch them
grep -q 'sdd-codex-dispatch' "$sdd/task-reviewer-prompt.md" && exit 1
grep -q 'sdd-codex-dispatch' "$sdd/re-review-prompt.md" && exit 1

# .git-writability hardening (cheap half): every adapter-side git call routes
# through the git_safe wrapper — replace refs ignored, fsmonitor/hooks/external
# diff neutralized — so a Sol-tampered repo config cannot turn verification into
# code execution or baseline substitution. No bare worktree git call remains.
grep -q 'git_safe()' scripts/sdd-codex-dispatch
grep -q 'GIT_NO_REPLACE_OBJECTS=1' scripts/sdd-codex-dispatch
grep -q 'core.fsmonitor=false' scripts/sdd-codex-dispatch
grep -q 'core.hooksPath=/dev/null' scripts/sdd-codex-dispatch
grep -qF 'git_safe -C "$worktree"' scripts/sdd-codex-dispatch
grep -qF 'git -C "$worktree"' scripts/sdd-codex-dispatch && exit 1

# Build-wave riders from the TDD-lane adjudication (ledger A13/A14/A15):
# codex runs in its own process group and the watchdog kills the group;
# the status parse is line-anchored (no mid-prose token match); the two
# preflight parses are nonfatal by construction with no head-under-pipefail.
# Negatives use the fail-closed form — a bare "! grep" is errexit-exempt
# under set -e and asserts nothing.
grep -qxF 'set -m' scripts/sdd-codex-dispatch
grep -qF 'kill -TERM -- "-$codex_pid"' scripts/sdd-codex-dispatch
grep -qF 'kill -KILL -- "-$codex_pid"' scripts/sdd-codex-dispatch
grep -qF "grep -E '^[[:space:]]*Status:" scripts/sdd-codex-dispatch
# A14 fix: the emphasis-strip must not delete underscores (status tokens carry them)
grep -qF "tr -d '*_" scripts/sdd-codex-dispatch && exit 1
grep -qF 'if ! codex_version=$(' scripts/sdd-codex-dispatch
grep -qF 'if ! auth_mode=$(' scripts/sdd-codex-dispatch
grep -q 'head -1' scripts/sdd-codex-dispatch && exit 1

echo "patch-2 intent checks: PASS"
