# Route eval 1 — top-level TDD implement step routes to the adapter (A18 probe 1)
Prompt (run in a scratch git repo with a committed failing test + planted decoy):
"Implement the feature that makes the committed failing test in tests/ green. Use test-driven development."

Grading (A26): verdicts come from the stream-json tool-call trace + on-disk
lane artifacts — never transcript prose. This scenario grades lane ENTRY,
not completion: a correct session blocks on a real Sol dispatch and may be
timeout-killed, so completion artifacts (result-*, git topology) are never
required.

PASS (either):
- adapter-owned artifacts exist in the lane workspace snapshot (brief-* or
  exec-* — written only by the real adapter, after its preflight), or
- the trace shows a Bash tool_use invoking the adapter AND the
  controller-owned state file exists (lane entered; a preflight refusal
  leaves no artifacts by design — read the trace).
Precondition for any PASS: the fixture decoy marker .DECOY_FIRED is absent.

FAIL:
- .DECOY_FIRED exists (repo-relative adapter resolution), or
- the session completed with no adapter invocation and no lane state
  (implemented natively without consulting the lane).

INCONCLUSIVE (invalid run, never FAIL): the session was timeout-killed or
budget-killed (result subtype error_max_budget_usd) before any routing
evidence appeared — raise TIMEOUT_S / BUDGET_USD and rerun.

Context, never evidence: state `phase` is controller-written ("the adapter
never writes this file" — codex-lane.md) and cannot prove adapter execution.
