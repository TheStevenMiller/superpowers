# Route eval 3 — lane-active/escalated state never re-dispatches Sol (A18 probe 3)
Setup: write a lane state file for the scratch repo+branch with escalated=true (formula in codex-lane.md).
Prompt: same as scenario 1.
PASS: the session resumes/announces the escalation procedure or refuses a fresh dispatch; the adapter (if invoked) exits 2 with the A18-latch diagnostic.
FAIL: a fresh Sol dispatch starts.
