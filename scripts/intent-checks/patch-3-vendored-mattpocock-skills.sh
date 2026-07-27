#!/usr/bin/env bash
# Intent checks for [local] patch #3 — vendored Matt Pocock coding skills.
#
# Run from the repo root; exit 0 means the patch's operative surface is
# intact. Checks follow the anchored form (operative-line anchor + negative
# assertion that the replaced mechanism is gone): a textual rebase that
# "applies" but strands the patch semantically must fail here. If any check
# fails after an upstream sync, do not promote — open the sync-blocked issue.
set -euo pipefail

# The four vendored reference skills exist with their operative surfaces:
# SKILL.md, the Codex surface (agents/openai.yaml), the MIT license copy,
# and the never-routed flag (reference files reached by explicit pointer)
for s in codebase-design tdd diagnosing-bugs design-an-interface; do
  test -f "skills/$s/SKILL.md"
  test -f "skills/$s/agents/openai.yaml"
  test -f "skills/$s/LICENSE"
  grep -q 'disable-model-invocation: true' "skills/$s/SKILL.md"
done

# Attribution pins the vendoring source (upstream re-sync is manual)
grep -q 'mattpocock/skills @ ed37663' skills/codebase-design/SKILL.md
grep -q 'mattpocock/skills @ ed37663' skills/tdd/SKILL.md
grep -q 'mattpocock/skills @ ed37663' skills/diagnosing-bugs/SKILL.md
grep -q 'only maintained copy' skills/design-an-interface/SKILL.md

# Site-1 pointer: brainstorming enters the deep-module/seam vocabulary at
# design time
grep -q 'codebase-design' skills/brainstorming/SKILL.md

# Site-2 pointers: writing-plans carries the deep-module check and the
# per-task test-seam declaration beside the Interfaces block
grep -q 'codebase-design' skills/writing-plans/SKILL.md
grep -qi 'test seam' skills/writing-plans/SKILL.md

# Cross-patch tripwire (§4): the reviewer prompt still carries the smell
# baseline patch #1 injected — the vendoring consumption chain assumes it
grep -qi 'smell' skills/subagent-driven-development/task-reviewer-prompt.md

# Deferred-by-design: to-tickets is NOT vendored (competes with
# writing-plans' core format)
test ! -d skills/to-tickets

echo "patch-3 intent checks: PASS"
