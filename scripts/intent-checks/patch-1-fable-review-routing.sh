#!/usr/bin/env bash
# Intent checks for [local] patch #1 — session-model review routing.
# (Filename kept at the original `patch-1-fable-review-routing.sh`: patch #4's
# sync glob and the release docs reference it by name. Renaming is a separate,
# sanctioned change — the routing itself is no longer Fable-specific.)
#
# Run from the repo root; exit 0 means the patch's operative surface is
# intact. Checks follow the anchored form (operative-line anchor + negative
# assertion that the replaced mechanism is gone): a textual rebase that
# "applies" but strands the patch semantically must fail here. If any check
# fails after an upstream sync, do not promote — open the sync-blocked issue.
#
# Coupling note: review lanes now inherit the SESSION model, so there is no
# CLAUDE_CODE_SUBAGENT_MODEL env pin left to track — it was removed from
# ~/.claude/settings.json in the same change. Re-introducing any env pin
# silently overrides these templates (the env value outranks frontmatter),
# which is why the pin and these checks moved together.
set -euo pipefail

sdd=skills/subagent-driven-development
rcr=skills/requesting-code-review

# Reviewer dispatch templates: the model line is deliberately ABSENT so the
# reviewer inherits the dispatching session's model. Both failure modes must
# fail here — upstream's placeholder restored by a textual rebase, and any
# re-pinned static model (the exact regression this patch now forbids).
! grep -qE '^[[:space:]]*model:' "$sdd/task-reviewer-prompt.md"
! grep -qE '^[[:space:]]*model:' "$sdd/re-review-prompt.md"
! grep -q 'MODEL — REQUIRED' "$sdd/task-reviewer-prompt.md"
! grep -q 'MODEL — REQUIRED' "$sdd/re-review-prompt.md"
grep -q 'session-model review routing' "$sdd/task-reviewer-prompt.md"
grep -q 'session-model review routing' "$sdd/re-review-prompt.md"

# SKILL.md Model Selection: Review-tasks row inherits the session model, the
# omit-is-deliberate carve-out is stated, and the scale-by-diff-size /
# cheap-to-mid re-review guidance stays gone
grep -q 'Review tasks.*inherit the session' "$sdd/SKILL.md"
grep -q 'EXCEPT the review lanes' "$sdd/SKILL.md"
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
