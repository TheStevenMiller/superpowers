# Codex Lane — the TDD implement step on GPT-5.6 Sol ([local] fork patch #5)

You (the top-level Claude Code session) are the lane **controller**: you
author and commit the tests, dispatch implementation to GPT-5.6 Sol through
the plugin's adapter, judge every verdict, and never delegate a gate. Sol
implements; a pinned read-only agent reviews; **the adapter is the trusted
committer** — Sol itself never commits and never writes git metadata (its
sandbox keeps `.git` read-only; grant nothing back).

Resolve the adapter absolutely before dispatching:
`<plugin-root>/scripts/tdd-codex-dispatch`, where `<plugin-root>` is two
levels up from this skill's announced base directory (the versioned plugin
cache dir). Never invoke it target-repo-relative — project repos do not
contain it.

## Commit topology (hard postcondition)

Every lane task ends as exactly TWO commits:
1. **RED test commit — this commit IS `base`.** You author ONE visible
   suite (spec examples + edge/adversarial + property/metamorphic tests
   wherever the domain supports them), format it, verify RED, and commit it
   through your normal commit path (real hooks run):

   ```
   test: <what the suite specifies>

   Tests-authored-by: Claude (Fable 5)
   Agent: test-author (Claude Fable 5)
   ```

2. **Implementation commit — exactly one parent, equal to `base`,** created
   by the adapter from Sol's worktree changes with your `--commit-subject`
   and the trailer `Agent: implementer (GPT 5.6 Sol)`. Fix rounds AMEND this
   commit; upheld contests/protected changes rebuild it on a re-frozen base.

There is never a third commit. Escalation provenance: amend Sol's commit to
`Agent: implementer (GPT 5.6 Sol; escalated: Claude Fable 5)`, or — when you
discard Sol's work — a fresh commit on `base` with
`Agent: implementer (Claude Fable 5)`.

## Lane workspace + state file

Workspace (out-of-repo, non-temp):
`~/.claude/tdd-lane/<repo-key>/<branch-key>/` where `repo-key` = first 16
hex chars of `sha256(absolute git common dir)` and `branch-key` = branch
with `/`→`_`. Compute it exactly like the adapter does:

```bash
common=$(git rev-parse --path-format=absolute --git-common-dir)
ws="$HOME/.claude/tdd-lane/$(printf '%s' "$common" | shasum -a 256 | awk '{print substr($1,1,16)}')/$(git rev-parse --abbrev-ref HEAD | tr '/' '_')"
mkdir -p "$ws"
```

State file `"$ws/state"` — flat `key=value` lines, controller-owned, written
ATOMICALLY (temp + rename) **before the RED commit** and updated at every
phase change:

```
repo=<absolute git common dir>
branch=<branch>
phase=<authoring|red-committed|dispatched|fix-N|escalated|terminal>
owner_pid=<your shell pid when dispatching, else last owner>
base_sha=<the RED commit — re-frozen on upheld contest/protected change>
fix_rounds_used=<0..3>
needs_context_used=<0|1>
malformed_used=<0|1>
g4_retry_used=<0|1>
green_infra_retry_used=<0|1>
probe_rounds_used=<0..2>
escalated=<true|false>
protect_path_N=<discovered literal path>   (one line per discovery)
protect_glob_N=<discovered glob>
```

Update pattern: `sed`/rewrite to `"$ws/state.tmp"`, then `mv "$ws/state.tmp" "$ws/state"`.
The adapter never writes this file: every transition (`base_sha` re-freeze,
`fix_rounds_used` bump, phase changes) is YOUR write — the adapter only
reads state for preflight and asserts byte-preservation on non-DONE exits.

## Controller entrypoint (run FIRST, every lane-relevant invocation)

Load `"$ws/state"` and branch — never route around this:

| State found | Action |
|---|---|
| none | new lane — proceed to step 1 |
| `phase=terminal` | finish cleanup (archive/delete state), then proceed as new |
| owner is you, phase non-terminal | resume at the recorded phase |
| adapter lock absent, or lock `pid` dead AND codex group empty (`kill -0 -- -<lock pgid>` fails) | crashed lane — explicit recovery: report the phase it died in; restore or roll back the RED commit (`git reset --soft HEAD~1` if RED must be undone); clean litter (the lock dir, `.git/index.lock`, dirty tree) before any retry |
| lock `pid` alive, or codex group non-empty | genuine live lane — REFUSE (one lane per repo+branch); never kill what you did not dispatch |
| `escalated=true` | resume the escalation procedure — NEVER a fresh Sol dispatch |

Manual recovery (operator command, after human review):
`cat "$ws/state"` to inspect; `rm -rf "$ws"` + `rm -rf "$(git rev-parse --path-format=absolute --git-dir)/tdd-dispatch.lock"` to clear a dead lane (the lock is a directory holding `pid`/`pgid` — kill the recorded codex group first if it still lives); repair `base_sha` only by re-freezing (never hand-edit to a guess).

## Lifecycle

1. **Preconditions.** Git repo; **named branch** (detached HEAD is refused);
   clean worktree; **pristine checkout** — no untracked
   `.gitignore`/`.gitattributes` anywhere, vendored/ignored trees included
   (a self-ignoring untracked ignore file could veil the protection floor,
   so the adapter's preflight refuses them — A12); check `fix_rounds_used`
   and every free-retry counter BEFORE any dispatch — at 3 fix rounds, go
   straight to escalation.
2. **Author the suite + discover protections.** One visible suite: spec
   examples, deliberate edge/adversarial tests, property/metamorphic tests
   wherever natural. Batch cap (G1): dispatch at most **12 collected logical
   cases** (parameterized cases count) and **400 changed nonblank test LOC**
   per round — split at feature seams into sequential batch-RED rounds when
   over. MANDATORY per-repo discovery: inventory the declared test
   command(s), runner configs, manifests, fixture/helper dirs, CI test jobs;
   record each as `protect_path_N`/`protect_glob_N` in state; pass them as
   `--protect-path`/`--protect-glob` (plain relative values — the adapter
   rejects pathspec magic; extensions can only ADD to the fixed floor).
   Inline-test repos: author in separate files at the ecosystem-idiomatic
   seam; a task genuinely requiring inline-test edits routes through
   PROTECTED_CHANGE_REQUESTED or falls back native. Doctests: out of scope.
3. **Format, then RED.** Run the repo's effective mutation-capable
   formatter/check chain over the test files FIRST (so commit-time
   re-staging is a no-op), then run the suite natively: every test must fail
   for a feature-shaped reason (assertion/missing behavior), never a
   collection/import accident. pytest repos: include `-p no:cacheprovider`
   in the suite command here AND in the brief's `Run them with:` line —
   pytest's cacheprovider writes a self-ignoring `.pytest_cache/.gitignore`
   that the veiled scan refuses (exit 4 after a full paid dispatch).
4. **RED commit = freeze `base`.** Commit through your normal path (real
   hooks run) with the §topology template. Hook probe: if the effective
   chain RUNS TESTS, `--no-verify` is permitted for this commit ONLY after
   manually executing the non-test hook obligations (formatter etc.).
   Mutation halves run BEFORE test halves in real chains: a commit the test
   half BLOCKS may still have reformatted and re-staged files — re-verify
   the tree after any refused commit.
   Update state: `phase=red-committed`, `base_sha=$(git rev-parse HEAD)`.
5. **Dispatch (background-only).** Write the brief (template below), then
   run the adapter via a background-capable execution path — NEVER
   foreground (600s cap vs the 3600s watchdog); act on the completion
   notification and read the persisted result record:

   ```bash
   printf '%s' "$BRIEF" | "<plugin-root>/scripts/tdd-codex-dispatch" \
     --worktree "$(git rev-parse --show-toplevel)" \
     --branch "$(git rev-parse --abbrev-ref HEAD)" \
     --commit-subject 'feat: <task summary>' \
     --effort medium \
     --protect-path <discovered>... --protect-glob <discovered>... \
     --format-cmd '<repo formatter over changed files, if any>'
   ```

   Fix rounds pass `--fix-round <N>` (the round number) on the same
   command. The flag follows topology, not round accounting: use it
   whenever Sol's implementation commit exists at HEAD — the adapter
   verifies exactly that and AMENDS the commit. Omit it whenever
   HEAD == `base_sha` (new lane; free re-dispatch on an untouched repo;
   upheld-contest/protected-change re-freeze) — initial-style preflight
   requires HEAD == base. NEVER re-freeze `base_sha` to clear an
   initial-style refusal while Sol's commit exists: that lands a second
   implementation commit and breaks the §topology postcondition.

   Cancellation = stopping the background task (the adapter's traps write a
   CANCELLED record). Update state `phase=dispatched` (or `fix-N`).
6. **Read the result.** Newest `result-*` file in `"$ws"`. No result file +
   nonzero/unknown exit ⇒ treat as KILLED: run the entrypoint's crashed-lane
   classification. Route by the table below.
7. **Native GREEN in a disposable checkout.** Never judge GREEN in the main
   worktree:

   ```bash
   co="$ws/green-$(git rev-parse --short HEAD)"
   git worktree add --detach "$co" HEAD
   # share dependency dirs explicitly (accepted exposure — deps are unprotected):
   # e.g. ln -s "$(git rev-parse --show-toplevel)/node_modules" "$co/node_modules"
   (cd "$co" && <suite command>)
   git worktree remove --force "$co"
   ```

   Assertion failure ⇒ fix-round row. Infrastructure failure (checkout,
   deps, runner crash) ⇒ one free retry, then BLOCKED — never a fix round.
8. **G4 adversarial review.** Dispatch the pinned `tdd-g4-reviewer` agent
   (fresh context every time) with this brief — absolute paths only:

   ```
   Review this TDD-lane implementation diff for hardcoding and
   letter-not-spirit hacks. Inputs:
   - diff artifact: <ws>/g4-diff-<dispatch-id>.patch
   - changed files: <ws>/g4-changed-files-<dispatch-id>.txt
   - RED-commit test files: <absolute paths>
   - protected-set manifest: the floor in
     <plugin-root>/scripts/tdd-codex-dispatch plus these extensions: <list>
   - task brief: <ws>/brief-<round>-<dispatch-id>.md
   Findings are capped to correctness. End with the TDD-G4-VERDICT block.
   ```

   Assert HEAD + porcelain unchanged before AND after the review dispatch.
   Parse the verdict fail-closed (exactly one header line in the whole
   reply, enforced BEFORE parsing — a quoted duplicate or a missing header
   is malformed; `verdict:` is the line after the header; empty output is
   malformed too):

   ```bash
   [ "$(grep -cx 'TDD-G4-VERDICT' "$verdict_file")" -eq 1 ] &&
   awk '/^TDD-G4-VERDICT$/ { if ((getline l) <= 0) exit 1;
     if (l ~ /^verdict: (PASS|FINDINGS|PROBE_REQUEST)$/) { sub(/^verdict: /, "", l); print l; exit 0 }
     exit 1 }' "$verdict_file"
   ```

   Malformed ⇒ one free format re-dispatch, then BLOCKED. FINDINGS ⇒ one fix
   round carrying the finding; recheck = fresh dispatch with prior findings
   as structured payload. PROBE_REQUEST ⇒ run the probe(s) yourself inside a
   fresh disposable checkout (NO repository writes), max 2 probe rounds per
   lane, then fresh recheck dispatch with the output as payload.
9. **G5 adjudication (you own the verdicts).**
   - TEST_CONTESTED upheld: fix the test on the Fable-owned base lineage —
     amend the RED commit (or replace it with a Fable commit IN ITS PLACE —
     never an additional commit), re-run format-then-RED, re-freeze
     `base_sha` in state, dispatch fresh initial-style. No round consumed.
   - PROTECTED_CHANGE_REQUESTED upheld: apply the change yourself on the
     base lineage, re-verify RED, re-freeze, dispatch fresh — free. If Sol's
     commit already exists (mid-fix), rebuild it onto the new base (rebase
     the single commit, or discard and re-dispatch initial-style).
   - Rejected or vague: one fix round consumed; the re-dispatch carries your
     rejection rationale.
10. **Finalize.** Two commits stand. Set `phase=terminal`, then clear or
    archive the state file. Run verification-before-completion as usual; the
    final summary records any escalation + trigger and forwards
    DONE_WITH_CONCERNS concerns.

## Transition table (authoritative — A14/A21)

Anchor rules: **no-evidence events never consume fix rounds** · **infra
failures get exactly one free retry, then BLOCKED** · **every no-record
completion routes through the crashed/killed classification** · **amend
re-dispatches (Sol's commit at HEAD) pass `--fix-round N`; initial-style
dispatches (HEAD == `base_sha`) omit it** · budget is checked BEFORE
dispatch (a 4th fix round is unreachable).

| Event | Round | Free retry | Next payload | State | Destination |
|---|---|---|---|---|---|
| DONE, all gates pass | — | — | — | phase=terminal, cleared | done |
| DONE_WITH_CONCERNS | — | — | — | — | gates as DONE; concerns → G4 input + final summary |
| GREEN assertion failure r1 | consumes | — | raw failure output | fix_rounds_used+1 | re-dispatch (amend) |
| GREEN assertion failure r2–3 | consumes | — | your conceptual analysis | fix_rounds_used+1 | re-dispatch; early-escalate on same-tests-same-reason as prior round |
| GREEN infra failure | no | 1 then BLOCKED | — | green_infra_retry_used=1 | re-run GREEN |
| Disposable-checkout create/cleanup failure | no | 1 then BLOCKED | — | — | retry once |
| G4 PASS | — | — | — | — | finalize |
| G4 finding (correctness) | consumes | — | the finding | fix_rounds_used+1 | re-dispatch; fresh recheck after |
| G4 PROBE_REQUEST | no | cap 2/lane | probe output | probe_rounds_used+1 | fresh recheck dispatch |
| Malformed G4 verdict | no | 1 then BLOCKED | format reminder | g4_retry_used=1 | re-dispatch reviewer |
| G4 dispatch failure | no | 1 then BLOCKED | — | g4_retry_used=1 | retry once |
| TEST_CONTESTED upheld | no | — | fresh initial brief | base_sha re-frozen | fresh dispatch |
| TEST_CONTESTED rejected/vague | consumes | — | rejection rationale | fix_rounds_used+1 | re-dispatch |
| PROTECTED_CHANGE_REQUESTED upheld | no | — | fresh brief (change applied) | base_sha re-frozen | fresh dispatch |
| PROTECTED_CHANGE_REQUESTED rejected/vague | consumes | — | rejection rationale | fix_rounds_used+1 | re-dispatch |
| NEEDS_CONTEXT (1st) | no | 1 | the missing context | needs_context_used=1 | free re-dispatch |
| NEEDS_CONTEXT (2nd) | no | — | — | — | BLOCKED ⇒ escalation |
| MALFORMED record (1st) | no | 1 | format reminder | malformed_used=1 | free re-dispatch |
| MALFORMED record (2nd) | no | — | — | — | BLOCKED ⇒ escalation |
| BLOCKED (Sol-reported) | terminal for Sol | — | — | escalated=true at entry | escalation |
| Exhaustion (fix_rounds_used=3 pre-dispatch) | — | — | — | escalated=true | escalation — no dispatch occurs |
| Adapter exit 2 (preflight; no record by design) | no | — | — | — | fix the environment or escalate |
| Adapter exit 3 + CANCELLED record | no | 1 if transient infra, then BLOCKED | — | — | verify A4 state, retry or BLOCKED |
| Completion, NO record (SIGKILL class) | never | — | — | — | crashed/killed classification (entrypoint) |
| Adapter exit 4 (postcondition, deviation named) | no | — | — | — | BLOCKED ⇒ recovery |
| Escalation entry (any path) | — | — | — | escalated=true BEFORE native work | escalation procedure |

## Escalation (inline, non-routing — you implement)

Set `escalated=true` in state FIRST. Then implement directly in the existing
worktree on the Fable-owned base lineage — implementation + verification
only; no re-classification, no routing decision, no adapter. Run the
IDENTICAL gates: commit everything; porcelain-clean assert; disposable-
checkout GREEN; G2 range diff (`git diff --name-only base..HEAD -- <the
protected pathspecs>` must be empty — run it with the four `GIT_*_PATHSPECS`
vars unset and `GIT_NO_REPLACE_OBJECTS=1`); G4 review via the pinned agent.
Provenance: amend Sol's commit with the co-implementation trailer, or a
fresh commit on `base` with the pure-Fable trailer (§topology). The final
summary records the escalation and its trigger.

## Brief template (§7 — the complete Sol contract)

Author the `Task:` statement as outcome only — NEVER command a
protected-space change (dep, manifest, config) in it. The tests express the
need and the Rules deny the means, forcing the PROTECTED_CHANGE_REQUESTED
route; a task line that commands the change reliably pulls the implementer
into making the edit directly, wasting a gate-caught dispatch.

```
# TDD Lane Dispatch — <task title>

Task: <one-paragraph task statement>

The committed failing tests are THE spec:
- <absolute test file path>
- <...>
Run them with: <suite command>

Implementation discipline (this brief is the complete contract — it
overrides any general TDD methodology you know):
- Write the simplest code that makes the given tests green.
- Add no behavior beyond what the tests specify.
- Refactor only while staying green.
- Respect the codebase's existing patterns and seams.

Rules:
- Tests and all test-adjacent files (test directories, runner configs,
  manifests, CI workflows, fixtures) are immutable, and creating new files
  there is also off-limits. Hardcoding or lookup tables keyed to test
  inputs = automatic fail; write general-purpose solutions.
- Work in the worktree only. Do NOT commit and do NOT run any git write
  operation — the harness commits your work.
- Remove scratch files; leave the tree ready to commit.
- Any exit other than DONE must leave the repository exactly as it was
  dispatched to you — revert your own experiments first. Deviations are
  rejected wholesale.

Statuses (exactly one; it is the line after TDD-DISPATCH-RESULT):
- DONE — every dispatched test green; run them and show the output.
- DONE_WITH_CONCERNS — green, plus concerns worth flagging (list them).
- BLOCKED — cannot proceed; say precisely why.
- NEEDS_CONTEXT — the brief lacks something; name it precisely.
- TEST_CONTESTED — a test is wrong: test id(s) + a specific claim of why +
  the expectation you believe correct. Vague contests cost a fix round;
  upheld ones are free.
- PROTECTED_CHANGE_REQUESTED — you need a protected-space change (dep,
  fixture, config): exact path(s) + rationale + the concrete change.

End your final message with exactly one report block:
TDD-DISPATCH-RESULT
status: <STATUS>
<what you implemented>
<test command + observed output>
<concerns / specifics for your status>

<round payload appended by the controller: raw failure output (round 1) /
conceptual analysis (rounds 2–3) / G4 finding / rejection rationale>
```

## Result-record vocabulary (what the adapter's record can say)

Six Sol statuses (above) plus two adapter-synthesized: `CANCELLED`
(timeout/interrupt/codex failure — never a fix round; one free retry when
transient, then BLOCKED) and `MALFORMED` (bad result block — one free
format-reminder re-dispatch, then BLOCKED). Exit 2 and exit 4 produce NO
record by design: exit 2 = your environment/preflight problem; exit 4 = a
postcondition deviation named on stderr — the reported status was NOT
accepted; treat as BLOCKED and run recovery.
