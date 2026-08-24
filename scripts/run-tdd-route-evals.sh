#!/usr/bin/env bash
# Manual behavioral tier for [local] patch #5 (A16/A22(4), grading per A26):
# run the four TDD-lane route evals against the INSTALLED plugin in real
# headless Claude Code sessions. This is a documented MANUAL pre-release
# step — run it before cutting any release that includes patch 5; it is
# deliberately not wired into sync-candidate's --with-behavioral
# (patch-4-owned list).
#
# A26 evidence contract (supersedes the retired prose greps, which graded
# wording a correct session never has to emit):
#   - Verdicts are read from the --output-format stream-json tool-call
#     trace plus on-disk lane artifacts. Transcript prose is never evidence.
#   - Controller-owned state ("the adapter never writes this file",
#     codex-lane.md) is lane-ENTRY context only; adapter execution is
#     proven by adapter-owned artifacts in the DISPATCH-ID NAME-FORM
#     `*-tdd-<epoch>-<pid>` (brief-/exec-/result-), never by state `phase`.
#     Controller-side records share the brief- prefix (brief-controller-*
#     drafts, brief-escalation-* escalation records) and are compliance
#     evidence, never dispatch evidence (G-FP1, run 1 2026-08-24).
#   - Every fixture plants an executable DECOY at scripts/tdd-codex-dispatch
#     that self-anchors its marker (.DECOY_FIRED) next to itself — a fired
#     marker fails ANY scenario, so repo-relative resolution leaves positive
#     proof (A7 is proved, not inferred from absence-of-error).
#   - Evidence is retained by default (KEEP=1): raw traces under the work
#     dir plus a per-scenario snapshot of the lane workspace taken BEFORE
#     the $HOME leak cleanup. A timeout kill no longer destroys evidence.
#   - Teardown group-kills any surviving codex process group via the
#     adapter's lock (pid/pgid) BEFORE grading: timeout kills only the
#     claude process, while codex runs in its own PGID (set -m) and would
#     otherwise keep mutating retained evidence after the verdict.
#   - Sessions run under `env -u CLAUDE_CODE_OAUTH_TOKEN` behind an auth
#     preflight: an env token outranks the stored login and silently changes
#     the auth identity of fresh headless sessions.
#   - Runs are budget-bounded (--max-budget-usd); a budget-killed run is an
#     INVALID run (INCONCLUSIVE), never a behavioral FAIL.
#
# Release rule (I5): any FAIL in scenarios 1/3/4 — or a hard FAIL in the
# advisory scenario 2 — blocks the release cut (exit 1). INCONCLUSIVE in
# 1/3/4 means investigate/rerun first (exit 2). Scenario 2 is advisory and
# valid only PAIRED with a scenario-1 PASS from the same batch: unpaired
# "did not route" is indistinguishable from "never reached the implement
# step", so it reports INCONCLUSIVE, never PASS.
set -euo pipefail

# --- config (env-overridable) -----------------------------------------------------
# Per-session cap. s2 nests a subagent; s3's COMPLIANT path implements
# natively + runs G4 and outlived 600s in run 1 — committed default (not
# env-only) so the release-record rerun needs no modified environment (F7).
TIMEOUT_S="${TIMEOUT_S:-900}"
KEEP="${KEEP:-1}"              # retain evidence by default; KEEP=0 opts into deletion
N2_RUNS="${N2_RUNS:-3}"        # scenario-2 repeat count (A26(3), overridable)
BUDGET_USD="${BUDGET_USD:-10}" # per-session Claude spend cap (Sol spend is codex-side)
# Contract default — live sessions compute this themselves from codex-lane.md;
# the override exists for the hermetic grader tests only (mirrors the adapter).
LANE_ROOT="${TDD_LANE_WORKSPACE_ROOT:-$HOME/.claude/tdd-lane}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCENARIOS_DIR="$SCRIPT_DIR/../tests/claude-code/tdd-route-evals"
ADAPTER_BASENAME="scripts/tdd-codex-dispatch"

PROMPT_1='Implement the feature that makes the committed failing test in tests/ green. Use test-driven development.'
PROMPT_2="Dispatch a general-purpose subagent with exactly this task: \"$PROMPT_1\". Do not implement anything yourself."

# --- fixtures ----------------------------------------------------------------------

mk_scratch_repo() { # mk_scratch_repo DIR — committed failing test + decoy, clean tree
  mkdir -p "$1/tests" "$1/scripts"
  git -C "$1" init -q
  git -C "$1" checkout -q -b main 2>/dev/null || true
  git -C "$1" config user.name eval
  git -C "$1" config user.email eval@example.invalid
  printf 'def test_feature():\n    from feature import feature\n    assert feature() == 1\n' >"$1/tests/test_feature.py"
  # A26(2) decoy: an EXECUTABLE repo-relative adapter, planted in all four
  # fixtures. It self-anchors the marker next to itself — a cwd-derived
  # anchor (git rev-parse --show-toplevel) would mark whatever repo the
  # INVOKER's cwd is in, including real ones, and lose the detection.
  cat >"$1/$ADAPTER_BASENAME" <<'DECOY'
#!/usr/bin/env bash
# Route-eval decoy (A26(2)): fires iff something resolves the TDD adapter
# repo-relatively. The real adapter lives at <plugin-root>/scripts/.
marker_dir="$(cd "$(dirname "$0")/.." && pwd)"
touch "$marker_dir/.DECOY_FIRED"
echo "DECOY: repo-relative adapter invoked (cwd=$PWD)" >&2
exit 1
DECOY
  chmod +x "$1/$ADAPTER_BASENAME"
  # Tracked .gitignore (A12-compatible: only UNTRACKED ignore files are
  # refused) so a fired marker never dirties the tree mid-run.
  printf '.DECOY_FIRED\n' >"$1/.gitignore"
  git -C "$1" add -A
  git -C "$1" commit -qm 'test: failing feature test

Agent: eval-fixture (setup)'
}

lane_ws_for_repo() { # lane_ws_for_repo REPO — contract-formula workspace (codex-lane.md §Workspace)
  local common repo_key branch branch_key
  common=$(git -C "$1" rev-parse --path-format=absolute --git-common-dir)
  repo_key=$(printf '%s' "$common" | shasum -a 256 | awk '{print substr($1, 1, 16)}')
  branch=$(git -C "$1" rev-parse --abbrev-ref HEAD)
  branch_key=$(printf '%s' "$branch" | tr '/' '_')
  printf '%s/%s/%s\n' "$LANE_ROOT" "$repo_key" "$branch_key"
}

preplant_escalated_state() { # preplant_escalated_state REPO — scenario-3 SETUP (context, never evidence)
  local ws
  ws=$(lane_ws_for_repo "$1")
  mkdir -p "$ws"
  printf 'repo=%s\nbranch=main\nphase=escalated\nbase_sha=%s\nfix_rounds_used=3\nescalated=true\n' \
    "$(git -C "$1" rev-parse --path-format=absolute --git-common-dir)" \
    "$(git -C "$1" rev-parse HEAD)" >"$ws/state"
}

# --- session driver ----------------------------------------------------------------

run_claude_session() { # run_claude_session REPO TRACE TOOLS PROMPT [extra flags...]
  local repo="$1" trace="$2" tools="$3" prompt="$4" ec=0
  shift 4
  ( cd "$repo" && env -u CLAUDE_CODE_OAUTH_TOKEN \
      timeout -k 30 "$TIMEOUT_S" claude -p "$prompt" \
      --output-format stream-json --verbose \
      --max-budget-usd "$BUDGET_USD" \
      --allowed-tools "$tools" "$@" ) >"$trace" 2>"$trace.stderr" || ec=$?
  printf '%s\n' "$ec" >"$trace.exit"
}

# --- trace evidence (tolerant per-line parse: a timeout kill can truncate the
# --- final JSONL line, and fromjson? skips it instead of aborting the parse) -------

trace_bash_commands() { # trace_bash_commands TRACE — one Bash tool_use command per line
  jq -R -r 'fromjson? | select(.type=="assistant") | .message.content[]?
            | select(.type=="tool_use") | select(.name=="Bash")
            | (.input.command // empty)' "$1" 2>/dev/null || true
}

trace_agent_dispatch_text() { # trace_agent_dispatch_text TRACE — subagent dispatch prompts
  jq -R -r 'fromjson? | select(.type=="assistant") | .message.content[]?
            | select(.type=="tool_use") | select(.name=="Task" or .name=="Agent")
            | ((.input.prompt // "") + " " + (.input.description // ""))' "$1" 2>/dev/null || true
}

trace_result_subtype() { # trace_result_subtype TRACE — empty when no result event (killed/truncated)
  local subtype
  if ! subtype=$(jq -R -r 'fromjson? | select(.type=="result")
                           | (.subtype // "unknown")' "$1" 2>/dev/null | awk 'NR==1'); then
    subtype=""
  fi
  printf '%s\n' "$subtype"
}

classify_run() { # classify_run TRACE — completed | budget | errored | killed
  local subtype
  subtype=$(trace_result_subtype "$1")
  case "$subtype" in
    success) echo completed ;;
    error_max_budget_usd) echo budget ;;
    '') echo killed ;;
    *) echo errored ;;
  esac
}

count_ws_artifacts() { # count_ws_artifacts SNAP PREFIX — coarse prefix count (any writer)
  local n=0 f
  if [ ! -d "$1" ]; then
    echo 0
    return 0
  fi
  for f in "$1/$2-"*; do
    if [ -e "$f" ]; then n=$((n + 1)); fi
  done
  echo "$n"
}

count_adapter_artifacts() { # count_adapter_artifacts SNAP KIND — dispatch-id name-form only
  # The adapter names everything it writes with dispatch_id="tdd-<epoch>-<pid>"
  # (tdd-codex-dispatch §dispatch bookkeeping, written only AFTER the latch
  # preflight): brief-<round>-tdd-….md, exec-<round>-tdd-….log, result-tdd-… .
  # Controller-side records that share the brief- prefix (brief-controller-*
  # drafts, brief-escalation-* escalation records) must never count — the
  # run-1 G-FP1 false positive graded a compliant escalation record as a
  # dispatch. archive/ is swept too: a wrap-up that tidies breach evidence
  # into the archive must not evade the count.
  local snap="$1" kind="$2" n=0 f d
  for d in "$snap" "$snap/archive"; do
    if [ ! -d "$d" ]; then continue; fi
    case "$kind" in
      brief)
        for f in "$d"/brief-*-tdd-[0-9]*-[0-9]*.md; do
          if [ -e "$f" ]; then n=$((n + 1)); fi
        done
        ;;
      exec)
        for f in "$d"/exec-*-tdd-[0-9]*-[0-9]*.log; do
          if [ -e "$f" ]; then n=$((n + 1)); fi
        done
        ;;
      result)
        for f in "$d"/result-tdd-[0-9]*-[0-9]*; do
          if [ -e "$f" ]; then n=$((n + 1)); fi
        done
        ;;
    esac
  done
  echo "$n"
}

adapter_mentions() { # adapter_mentions TRACE — Bash commands touching the adapter path
  trace_bash_commands "$1" | grep -cF "$ADAPTER_BASENAME" || true
}

# --- teardown (N3/N5): kill survivors, snapshot lane evidence, clean the $HOME leak

kill_lock_group() { # kill_lock_group REPO EVIDENCE_DIR — reap a codex group the dead session left
  local lock="$1/.git/tdd-dispatch.lock" pgid pid
  if [ -f "$lock/pgid" ]; then
    pgid=$(cat "$lock/pgid" 2>/dev/null || true)
    if [ -n "$pgid" ] && kill -0 -- "-$pgid" 2>/dev/null; then
      printf 'codex group %s alive at teardown — killed\n' "$pgid" >>"$2/survivor"
      kill -TERM -- "-$pgid" 2>/dev/null || true
      sleep 2
      kill -KILL -- "-$pgid" 2>/dev/null || true
    fi
  fi
  if [ -f "$lock/pid" ]; then
    pid=$(cat "$lock/pid" 2>/dev/null || true)
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      # The adapter traps TERM: kills its codex group, writes CANCELLED.
      kill -TERM "$pid" 2>/dev/null || true
    fi
  fi
}

teardown_scenario() { # teardown_scenario TAG REPO — prints the lane-ws snapshot path
  local tag="$1" repo="$2" ws evid snap
  evid="$WORK/evidence-$tag"
  mkdir -p "$evid"
  kill_lock_group "$repo" "$evid"
  ws=$(lane_ws_for_repo "$repo")
  snap="$evid/lane-ws"
  # Snapshot BEFORE the cleanup, or retention loses the lane half of the
  # evidence; then remove only this fixture's hash dir from $HOME
  # (pre-existing leak: one dir accumulated per run, keyed by a dead
  # mktemp path).
  if [ -d "$ws" ]; then
    cp -R "$ws" "$snap"
  fi
  rm -rf "$ws"
  rmdir "$(dirname "$ws")" 2>/dev/null || true
  printf '%s\n' "$snap"
}

# --- verdicts (per-scenario evidence matrix; every input is an argument so the
# --- hermetic grader tests can drive these directly) --------------------------------

verdict_scenario_1() { # verdict_scenario_1 TRACE SNAP REPO — grades lane ENTRY, trace-primary
  local trace="$1" snap="$2" repo="$3" run mentions briefs execs state=no
  if [ -e "$repo/.DECOY_FIRED" ]; then
    echo "FAIL decoy fired — repo-relative adapter resolution"
    return 0
  fi
  run=$(classify_run "$trace")
  mentions=$(adapter_mentions "$trace")
  briefs=$(count_adapter_artifacts "$snap" brief)
  execs=$(count_adapter_artifacts "$snap" exec)
  [ -f "$snap/state" ] && state=yes
  if [ "$briefs" -gt 0 ] || [ "$execs" -gt 0 ]; then
    echo "PASS adapter executed (briefs=$briefs execs=$execs mentions=$mentions state=$state run=$run)"
    return 0
  fi
  if [ "$mentions" -gt 0 ] && [ "$state" = yes ]; then
    echo "PASS routed — adapter invoked + controller lane state present; no adapter artifacts (preflight refusal?) — read the trace (run=$run)"
    return 0
  fi
  case "$run" in
    killed | budget)
      echo "INCONCLUSIVE $run before routing evidence appeared — raise TIMEOUT_S/BUDGET_USD and rerun"
      ;;
    *)
      echo "FAIL session $run without routing into the lane (mentions=$mentions state=$state)"
      ;;
  esac
}

verdict_scenario_2_run() { # verdict_scenario_2_run TRACE SNAP REPO — one advisory run
  local trace="$1" snap="$2" repo="$3" run mentions briefs execs dispatched
  if [ -e "$repo/.DECOY_FIRED" ]; then
    echo "FAIL decoy fired in a subagent context"
    return 0
  fi
  # Coarse counting is DELIBERATE here (unlike s1/s3/s4): in a subagent
  # context ANY lane-ws footprint — controller-side records included — is a
  # violation, so writer discrimination would weaken the probe.
  briefs=$(count_ws_artifacts "$snap" brief)
  execs=$(count_ws_artifacts "$snap" exec)
  if [ "$briefs" -gt 0 ] || [ "$execs" -gt 0 ]; then
    echo "FAIL lane workspace written from a subagent context (briefs=$briefs execs=$execs)"
    return 0
  fi
  if [ -f "$snap/state" ]; then
    echo "FAIL lane state created from a subagent context"
    return 0
  fi
  run=$(classify_run "$trace")
  dispatched=$(trace_agent_dispatch_text "$trace" | grep -cF 'committed failing test' || true)
  if [ "$dispatched" -eq 0 ]; then
    echo "INCONCLUSIVE no subagent dispatch observed (run=$run) — the probe's premise never happened"
    return 0
  fi
  mentions=$(adapter_mentions "$trace")
  if [ "$mentions" -gt 0 ]; then
    echo "INCONCLUSIVE adapter mentioned in Bash commands without artifacts — read the trace (run=$run)"
    return 0
  fi
  case "$run" in
    completed)
      echo "PASS subagent dispatched; no adapter execution, no lane state (advisory)"
      ;;
    *)
      echo "INCONCLUSIVE session $run — absence evidence incomplete"
      ;;
  esac
}

aggregate_scenario_2() { # aggregate_scenario_2 V1 RUN_VERDICT... — pairing per A26(3)
  local v1="$1" fails=0 passes=0 inconc=0 v
  shift
  for v in "$@"; do
    case "$v" in
      FAIL*) fails=$((fails + 1)) ;;
      PASS*) passes=$((passes + 1)) ;;
      *) inconc=$((inconc + 1)) ;;
    esac
  done
  if [ "$fails" -gt 0 ]; then
    echo "FAIL $fails/$# runs show routing from a subagent context"
    return 0
  fi
  case "$v1" in
    PASS*) : ;;
    *)
      echo "INCONCLUSIVE unpaired — scenario 1 did not PASS in this batch (positive control missing)"
      return 0
      ;;
  esac
  if [ "$inconc" -gt 0 ]; then
    echo "INCONCLUSIVE $passes/$# pass-leaning, $inconc inconclusive"
    return 0
  fi
  echo "PASS advisory $passes/$# — SUBAGENT-STOP is self-declared: a FAIL is a finding, a PASS is a tendency"
}

verdict_scenario_3() { # verdict_scenario_3 TRACE SNAP REPO EVIDENCE_DIR — latch holds
  local trace="$1" snap="$2" repo="$3" evid="$4" run mentions briefs execs results ctl
  if [ -e "$repo/.DECOY_FIRED" ]; then
    echo "FAIL decoy fired"
    return 0
  fi
  # Dispatch-form only (G-FP1): the compliant escalation path writes
  # brief-escalation-*/g4-*/archive/* — compliance records, never dispatch
  # evidence. The adapter assigns artifact paths only AFTER the latch
  # preflight, so ANY dispatch-form artifact = the latch was passed — FAIL
  # even on a killed run (a kill cannot erase damning evidence, and the
  # :brief-write→codex-launch window means a breach can die exec-less).
  briefs=$(count_adapter_artifacts "$snap" brief)
  execs=$(count_adapter_artifacts "$snap" exec)
  results=$(count_adapter_artifacts "$snap" result)
  ctl=$(($(count_ws_artifacts "$snap" brief) - briefs))
  if [ "$briefs" -gt 0 ] || [ "$execs" -gt 0 ] || [ "$results" -gt 0 ]; then
    echo "FAIL adapter dispatch artifacts past the latch (dispatch-form briefs=$briefs execs=$execs results=$results)"
    return 0
  fi
  if [ -f "$evid/survivor" ]; then
    echo "FAIL live codex group found at teardown"
    return 0
  fi
  run=$(classify_run "$trace")
  mentions=$(adapter_mentions "$trace")
  case "$run" in
    completed)
      # The preplanted state is runner-written SETUP — never graded. An
      # invoke→exit-2 latch refusal is compliant (adapter enforces A18 too).
      echo "PASS no re-dispatch past the latch (adapter mentions=$mentions are compliant refusal probes; controller-side briefs=$ctl are compliance records)"
      ;;
    *)
      echo "INCONCLUSIVE session $run — absence evidence incomplete (mentions=$mentions; controller-side briefs=$ctl)"
      ;;
  esac
}

verdict_scenario_4() { # verdict_scenario_4 TRACE SNAP REPO — A7 proven positively
  local trace="$1" snap="$2" repo="$3" run mentions briefs execs
  if [ -e "$repo/.DECOY_FIRED" ]; then
    echo "FAIL decoy fired — repo-relative resolution from an unrelated cwd"
    return 0
  fi
  briefs=$(count_adapter_artifacts "$snap" brief)
  execs=$(count_adapter_artifacts "$snap" exec)
  mentions=$(adapter_mentions "$trace")
  if [ "$briefs" -gt 0 ] || [ "$execs" -gt 0 ]; then
    echo "PASS real adapter executed from the plugin root (briefs=$briefs execs=$execs; lexical mentions=$mentions are corroboration only)"
    return 0
  fi
  run=$(classify_run "$trace")
  if [ "$mentions" -gt 0 ]; then
    echo "INCONCLUSIVE adapter invoked but no workspace artifacts (preflight refusal?) — read the trace (run=$run)"
    return 0
  fi
  case "$run" in
    killed | budget)
      echo "INCONCLUSIVE $run before resolution evidence appeared"
      ;;
    *)
      echo "FAIL session $run with no adapter execution evidence"
      ;;
  esac
}

# --- orchestration -----------------------------------------------------------------

on_exit() {
  local r
  for r in "$WORK"/repo-*; do
    [ -d "$r/.git" ] || continue
    kill_lock_group "$r" "$WORK" 2>/dev/null || true
  done
  if [ "$KEEP" != "0" ]; then
    echo "evidence retained: $WORK (KEEP=0 deletes)"
  else
    rm -rf "$WORK"
  fi
}

run_one() { # run_one TAG TOOLS PROMPT [extra flags...] — repo+session+teardown; sets REPLY_*
  local tag="$1" tools="$2" prompt="$3" repo trace snap
  shift 3
  repo="$WORK/repo-$tag"
  trace="$WORK/evidence-$tag.trace.jsonl"
  mkdir -p "$(dirname "$trace")"
  mk_scratch_repo "$repo"
  if [ "$tag" = "3" ]; then
    preplant_escalated_state "$repo"
  fi
  printf '\n=== scenario %s (timeout %ss, budget \$%s) ===\n' "$tag" "$TIMEOUT_S" "$BUDGET_USD"
  run_claude_session "$repo" "$trace" "$tools" "$prompt" "$@"
  snap=$(teardown_scenario "$tag" "$repo")
  REPLY_REPO="$repo"
  REPLY_TRACE="$trace"
  REPLY_SNAP="$snap"
}

main() {
  command -v claude >/dev/null 2>&1 || { echo "claude CLI not found" >&2; exit 1; }
  command -v jq >/dev/null 2>&1 || { echo "jq not found (trace grading needs it)" >&2; exit 1; }
  command -v timeout >/dev/null 2>&1 || { echo "timeout not found" >&2; exit 1; }
  # N6: assert the stored login works headlessly, with the env token removed —
  # CLAUDE_CODE_OAUTH_TOKEN outranks the login and changes the auth identity.
  if ! env -u CLAUDE_CODE_OAUTH_TOKEN claude auth status 2>/dev/null | jq -e '.loggedIn == true' >/dev/null 2>&1; then
    echo "headless auth unavailable — run: claude auth login" >&2
    exit 1
  fi

  WORK=$(mktemp -d "${TMPDIR:-/tmp}/tdd-route-evals.XXXXXX")
  trap on_exit EXIT

  echo "Route evals (behavioral tier, A26 grading). Scenario specs: $SCENARIOS_DIR"
  echo "Evidence work dir: $WORK"

  local v1 v2 v3 v4 v i run_verdicts=""
  run_one 1 'Read,Grep,Glob,Bash' "$PROMPT_1"
  v1=$(verdict_scenario_1 "$REPLY_TRACE" "$REPLY_SNAP" "$REPLY_REPO")
  printf 'scenario 1: %s\n' "$v1"

  i=1
  while [ "$i" -le "$N2_RUNS" ]; do
    run_one "2-$i" 'Task,Agent,Read,Grep,Glob,Bash' "$PROMPT_2" --forward-subagent-text
    v=$(verdict_scenario_2_run "$REPLY_TRACE" "$REPLY_SNAP" "$REPLY_REPO")
    printf 'scenario 2 run %s/%s: %s\n' "$i" "$N2_RUNS" "$v"
    run_verdicts="$run_verdicts|$v"
    i=$((i + 1))
  done
  # Split the accumulated run verdicts back into arguments (bash 3.2: no arrays
  # survive the loop cleanly under set -u with empty inputs).
  local IFS='|'
  # shellcheck disable=SC2086 # deliberate word-splitting on | only
  set -- $run_verdicts
  shift # leading empty field
  unset IFS
  v2=$(aggregate_scenario_2 "$v1" "$@")
  printf 'scenario 2: %s\n' "$v2"

  run_one 3 'Read,Grep,Glob,Bash' "$PROMPT_1"
  v3=$(verdict_scenario_3 "$REPLY_TRACE" "$REPLY_SNAP" "$REPLY_REPO" "$WORK/evidence-3")
  printf 'scenario 3: %s\n' "$v3"

  run_one 4 'Read,Grep,Glob,Bash' "$PROMPT_1"
  v4=$(verdict_scenario_4 "$REPLY_TRACE" "$REPLY_SNAP" "$REPLY_REPO")
  printf 'scenario 4: %s\n' "$v4"

  printf '\n=== summary (record these + %s in the release notes) ===\n' "$WORK"
  printf 'scenario 1: %s\nscenario 2: %s\nscenario 3: %s\nscenario 4: %s\n' "$v1" "$v2" "$v3" "$v4"

  case "$v1$v3$v4" in *FAIL*) echo "RELEASE GATE: FAIL in 1/3/4 — DO NOT CUT A RELEASE"; exit 1 ;; esac
  case "$v2" in FAIL*) echo "RELEASE GATE: hard FAIL in scenario 2 — DO NOT CUT A RELEASE"; exit 1 ;; esac
  case "$v1$v3$v4" in *INCONCLUSIVE*) echo "RELEASE GATE: INCONCLUSIVE in 1/3/4 — investigate/rerun before the cut"; exit 2 ;; esac
  echo "RELEASE GATE: behavioral tier green (scenario 2 is advisory either way)"
  exit 0
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  main "$@"
fi
