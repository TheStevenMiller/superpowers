#!/usr/bin/env bash
# Intent checks for [local] patch #5 — TDD codex lane.
#
# Run from the repo root; exit 0 means the patch's operative surface is
# intact. Anchored form per F04: operative-line anchors + negative
# assertions that banned mechanisms stay gone. A textual rebase that
# "applies" but strands the patch semantically must fail here. If any check
# fails after an upstream sync, do not promote — open the sync-blocked issue.
# Negative assertions use the fail-closed form "grep … && exit 1" — a bare
# "! grep" is exempt from set -e and asserts nothing.
set -euo pipefail

tdd=skills/test-driven-development
adapter=scripts/tdd-codex-dispatch

# The adapter exists and keeps its operative guarantees: direct codex exec,
# pinned Sol model, workspace-write sandbox with NO git-metadata grant
# (trusted-parent commit — Sol never commits), exact implementer trailer,
# fresh-only dispatch, group kill, anchored result parse, typed protection
# flags with top-anchored encodings, pathspec-env immunity
test -x "$adapter"
grep -q 'codex exec' "$adapter"
grep -q 'gpt-5.6-sol' "$adapter"
grep -q 'workspace-write' "$adapter"
grep -q -- '--add-dir' "$adapter" && exit 1
grep -qF 'Agent: implementer (GPT 5.6 Sol)' "$adapter"
grep -q -- '--resume' "$adapter" && exit 1
grep -qxF 'set -m' "$adapter"
grep -qF 'kill -TERM -- "-$codex_pid"' "$adapter"
grep -q 'TEST_CONTESTED' "$adapter"
grep -q 'PROTECTED_CHANGE_REQUESTED' "$adapter"
grep -qF ':(glob,top)' "$adapter"
grep -qF ':(literal,top)' "$adapter"
grep -q -- '--protect-path' "$adapter"
grep -q -- '--protect-glob' "$adapter"
grep -q 'GIT_NO_REPLACE_OBJECTS=1' "$adapter"
grep -q 'core.hooksPath=/dev/null' "$adapter"
grep -qF 'grep -cx '\''TDD-DISPATCH-RESULT'\''' "$adapter"
grep -qF 'rev-list --parents -n1 HEAD' "$adapter"
grep -q 'HEAD\^' "$adapter" && exit 1
grep -q 'head -1' "$adapter" && exit 1
grep -q 'CODEX_PROGRESS_HOOK_ENABLED=false' "$adapter"

# SKILL.md loop-entry block: full-cycle handoff before native RED, absolute
# adapter resolution, advisory-honesty sentence; native loop untouched below
grep -qF 'references/codex-lane.md' "$tdd/SKILL.md"
grep -qF '<plugin-root>/scripts/tdd-codex-dispatch' "$tdd/SKILL.md"
grep -q 'behaviorally evaluated but advisory' "$tdd/SKILL.md"
grep -q 'top-level session (not a subagent)' "$tdd/SKILL.md"

# Lane contract doc: topology, state, transition table, escalation, recovery
test -f "$tdd/references/codex-lane.md"
grep -q 'exactly one parent, equal to `base`' "$tdd/references/codex-lane.md"
grep -q 'Transition table' "$tdd/references/codex-lane.md"
# Trailer grammar: the Claude-side model is the SESSION's, never a fixed
# name (Sol's implementer trailer stays literal — the engine is the premise).
# Negative half: any re-pinned Fable spelling must fail the sync.
grep -qF 'Agent: implementer (GPT 5.6 Sol; escalated: Claude <session model>)' "$tdd/references/codex-lane.md"
grep -qF 'Tests-authored-by: Claude (<session model>)' "$tdd/references/codex-lane.md"
grep -qF 'Agent: test-author (Claude <session model>)' "$tdd/references/codex-lane.md"
grep -qF 'Agent: implementer (GPT 5.6 Sol)' "$tdd/references/codex-lane.md"
grep -qE 'Claude Fable 5|Claude \(Fable 5\)|pure-Fable|Fable-owned' "$tdd/references/codex-lane.md" && exit 1
grep -q 'escalated=true' "$tdd/references/codex-lane.md"
grep -q 'Manual recovery' "$tdd/references/codex-lane.md"

# Pinned reviewer agent: the tools line IS the enforcement (A17/A23(5)) —
# anchor it exactly; widening the tool list must fail the sync
grep -qxF 'tools: Read, Grep, Glob' agents/tdd-g4-reviewer.md
grep -qxF 'name: tdd-g4-reviewer' agents/tdd-g4-reviewer.md
grep -q 'TDD-G4-VERDICT' agents/tdd-g4-reviewer.md

# Behavioral-tier artifacts exist (run manually pre-release, never here)
test -x scripts/run-tdd-route-evals.sh
test -f tests/claude-code/tdd-route-evals/scenario-1-toplevel-routes.md
test -f tests/claude-code/tdd-route-evals/scenario-2-subagent-no-route.md
test -f tests/claude-code/tdd-route-evals/scenario-3-latch-no-redispatch.md
test -f tests/claude-code/tdd-route-evals/scenario-4-unrelated-cwd.md

# The fixture suite is the mechanical tier — run it (A16(2): every sync AND
# every promote execute the adapter fixtures; ~90s, stubbed codex only)
bash tests/claude-code/test-tdd-codex-dispatch.sh

echo "patch-5 intent checks: PASS"
