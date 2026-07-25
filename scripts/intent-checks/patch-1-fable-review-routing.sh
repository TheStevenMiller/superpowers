#!/usr/bin/env bash
# Intent checks for [local] patch #1 — Fable review routing.
#
# Run from the repo root; exit 0 means the patch's operative surface is
# intact. Checks follow the anchored form (operative-line anchor + negative
# assertion that the replaced mechanism is gone): a textual rebase that
# "applies" but strands the patch semantically must fail here. If any check
# fails after an upstream sync, do not promote — open the sync-blocked issue.
#
# Coupling note: the `fable` value tracks the CLAUDE_CODE_SUBAGENT_MODEL env
# pin (~/.claude/settings.json). If that pin moves, this patch and these
# checks move with it.
set -euo pipefail

sdd=skills/subagent-driven-development
rcr=skills/requesting-code-review

# Reviewer dispatch templates: placeholder sites resolved to the static
# Fable pin (template model line + Placeholders legend, in each file)
grep -q 'model: fable' "$sdd/task-reviewer-prompt.md"
grep -q 'model: fable' "$sdd/re-review-prompt.md"
! grep -q 'MODEL — REQUIRED' "$sdd/task-reviewer-prompt.md"
! grep -q 'MODEL — REQUIRED' "$sdd/re-review-prompt.md"

# SKILL.md Model Selection: Review-tasks row is static-fable; the
# scale-by-diff-size / cheap-to-mid re-review guidance is gone
grep -q 'Review tasks.*fable' "$sdd/SKILL.md"
! grep -q 'cheap-to-mid' "$sdd/SKILL.md"
! grep -q 'cheap-to-mid' "$sdd/re-review-prompt.md"

# code-review two-axis injection (smell baseline) present in the task
# reviewer and the final whole-branch reviewer; the scoped re-review
# deliberately carries NO injection (it verifies findings, it doesn't
# re-review)
grep -q 'Standards baseline' "$sdd/task-reviewer-prompt.md"
grep -q 'Standards baseline' "$rcr/code-reviewer.md"
! grep -q 'Standards baseline' "$sdd/re-review-prompt.md"

echo "patch-1 intent checks: PASS"
