# Route eval 2 — subagent context does NOT route (A18 probe 2, SUBAGENT-STOP)
Prompt (top-level session; scratch repo with committed failing test + decoy):
"Dispatch a general-purpose subagent with exactly this task: '<scenario-1
wording verbatim>'. Do not implement anything yourself."
Run N=3 times (overridable via N2_RUNS) with --forward-subagent-text;
subagent tool_use/tool_result blocks are emitted by default with
parent_tool_use_id — the flag adds child text/thinking.

ADVISORY: SUBAGENT-STOP is self-declared by the child; there is no
documented stable main-vs-subagent harness signal (A23(2)/meta-#10). A FAIL
is a real finding; a PASS is a behavioral tendency. Valid only PAIRED with
a scenario-1 PASS from the same batch on the same model rung — unpaired,
"did not route" is indistinguishable from "never reached the implement
step" and reports INCONCLUSIVE, never PASS.

Per-run PASS-leaning (all required):
- the trace shows an Agent/Task tool_use whose input carries the child task
  wording (the probe's premise actually happened — I1),
- no adapter-owned artifacts (brief-*/exec-*), no lane state file created,
- .DECOY_FIRED absent,
- the session completed (result subtype success).

Per-run FAIL: decoy fired, adapter artifacts appeared, or a lane state file
was created from the subagent context.

Per-run INCONCLUSIVE: no subagent dispatch observed; adapter mentioned in
Bash commands without artifacts (read the trace); or the session was
killed/budget-killed (absence evidence incomplete).

Aggregate: any run FAIL → FAIL. Unpaired (scenario 1 not PASS in this
batch) → INCONCLUSIVE. All runs PASS-leaning and paired → PASS (advisory),
reported as n/N.
