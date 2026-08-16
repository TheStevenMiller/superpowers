# Route eval 4 — unrelated-cwd route smoke (A7)
Setup: run the scenario-1 prompt from a FRESH fixture repo in a different directory that has never seen the plugin (no scripts/ dir of its own).
PASS: the lane resolves the adapter at <plugin-root>/scripts/tdd-codex-dispatch (absolute); no "command not found: scripts/tdd-codex-dispatch".
FAIL: any bare/repo-relative invocation attempt.
