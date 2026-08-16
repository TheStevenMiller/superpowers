# Route eval 2 — subagent context does NOT route (A18 probe 2, SUBAGENT-STOP)
Prompt: dispatch a general-purpose subagent whose task says "implement this small feature with TDD" in the same scratch repo.
PASS: the subagent uses the native TDD loop; no tdd-codex-dispatch invocation appears.
FAIL: the subagent shells out to the adapter.
