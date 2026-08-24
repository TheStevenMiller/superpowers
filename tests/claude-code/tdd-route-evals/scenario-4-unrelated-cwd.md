# Route eval 4 — unrelated-cwd route smoke (A7)
Setup: run the scenario-1 prompt from a FRESH fixture repo under its own
mktemp root that shares no ancestor with the fork clone or the plugin
cache. The fixture plants the executable decoy at
scripts/tdd-codex-dispatch: it self-anchors a .DECOY_FIRED marker next to
itself and exits 1, so repo-relative resolution leaves positive proof (the
old criterion — absence of "command not found" — was satisfied equally by a
session that never reached the implement step).

Grading (A26): A7 is proven POSITIVELY. A lexical match of the absolute
plugin path in the trace is corroboration only — a command string can
mention a path it does not execute, and a correct invocation through a
variable can lack the literal (I2). Proof of resolution = the REAL adapter
executed (its workspace artifacts exist) while the decoy stayed unfired.

PASS (all required):
- adapter-owned artifacts exist in the lane workspace snapshot
  (brief-*/exec-*), and
- .DECOY_FIRED absent.
Trace mentions of the adapter path are reported as corroborating
diagnostics.

FAIL: the decoy fired (repo-relative resolution), or the session completed
with no adapter execution evidence.

INCONCLUSIVE: adapter invoked in the trace but no workspace artifacts
(preflight refusal — read the trace); or the session was
killed/budget-killed before resolution evidence appeared.

Note: the local.4 live e2e already exercised adapter resolution FROM the
cache location; this scenario adds the unrelated-CWD half.
