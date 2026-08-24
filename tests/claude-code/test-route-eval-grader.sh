#!/usr/bin/env bash
# Hermetic grader tests for scripts/run-tdd-route-evals.sh ([local] patch #5,
# Task 19 / ledger A26-I6). The 2026-08-21 tier execution proved the verdict
# logic is the component that fails silently — so it gets a deterministic
# seam: canned stream-json traces, canned lane-workspace snapshots, and
# scratch git repos drive lane_ws_for_repo() and every verdict_* function
# with NO claude, NO codex, NO network. Budget: whole suite well under 15s —
# it runs at every sync and every promote via patch-5's intent check.
set -uo pipefail # deliberately NOT -e: probes assert on expected failures

SUITE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SUITE_DIR/../.." && pwd)"
RUNNER="$REPO_ROOT/scripts/run-tdd-route-evals.sh"

export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE 2>/dev/null || true

TMP_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/route-eval-grader.XXXXXX")
cleanup() { rm -rf "$TMP_ROOT"; }
trap cleanup EXIT

# The runner honors TDD_LANE_WORKSPACE_ROOT (mirroring the adapter) so these
# tests never touch the real $HOME lane root.
export TDD_LANE_WORKSPACE_ROOT="$TMP_ROOT/lane"

PASS=0
FAIL=0
ok() { PASS=$((PASS + 1)); printf '  [PASS] %s\n' "$1"; }
bad() {
  FAIL=$((FAIL + 1))
  printf '  [FAIL] %s\n' "$1"
  shift
  local l
  for l in "$@"; do printf '         %s\n' "$l"; done
}
summary() {
  printf '\ntest-route-eval-grader: %d passed, %d failed\n' "$PASS" "$FAIL"
  [ "$FAIL" -eq 0 ]
}

# in_runner FN ARGS... — run one sourced-runner function in a subshell
in_runner() {
  (
    # shellcheck disable=SC1090
    source "$RUNNER"
    "$@"
  )
}

mk_repo() { # mk_repo DIR [BRANCH]
  local dir="$1" branch="${2:-main}"
  mkdir -p "$dir"
  git -C "$dir" init -q
  git -C "$dir" checkout -q -b "$branch" 2>/dev/null || true
  git -C "$dir" config user.name t
  git -C "$dir" config user.email t@example.invalid
  git -C "$dir" -c core.hooksPath=/dev/null commit -q --allow-empty -m x
}

# Canned stream-json lines (shape pinned against a real 2.1.240 trace).
LINE_BASH_ABS='{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash","input":{"command":"printf %s \"$BRIEF\" | \"/abs/plugins/cache/superpowers/scripts/tdd-codex-dispatch\" --protect-path tests"}}]}}'
LINE_BASH_PLAIN='{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash","input":{"command":"git status --porcelain"}}]}}'
LINE_READ='{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Read","input":{"file_path":"scripts/tdd-codex-dispatch"}}]}}'
LINE_TASK='{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Task","input":{"prompt":"Implement the feature that makes the committed failing test in tests/ green. Use test-driven development.","description":"child TDD task"}}]}}'
LINE_AGENT='{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Agent","input":{"prompt":"Implement the feature that makes the committed failing test in tests/ green.","description":"child"}}]}}'
LINE_RESULT_OK='{"type":"result","subtype":"success","is_error":false}'
LINE_RESULT_BUDGET='{"type":"result","subtype":"error_max_budget_usd","is_error":true}'
LINE_RESULT_OTHER='{"type":"result","subtype":"error_during_execution","is_error":true}'

mk_trace() { # mk_trace FILE LINE... — one canned event per line
  local file="$1"
  shift
  : >"$file"
  local l
  for l in "$@"; do printf '%s\n' "$l" >>"$file"; done
}

mk_snap() { # mk_snap DIR [ARTIFACT...] — canned lane-ws snapshot
  local dir="$1"
  shift
  mkdir -p "$dir"
  local a
  for a in "$@"; do : >"$dir/$a"; done
}

# --- lane_ws_for_repo: contract formula (C1 regression) ---------------------------

probe_lane_ws_formula() {
  echo "lane_ws_for_repo (C1):"
  local r="$TMP_ROOT/repo-main" got want common
  mk_repo "$r"
  got=$(in_runner lane_ws_for_repo "$r")
  # Independent computation, verbatim from codex-lane.md §Workspace.
  common=$(git -C "$r" rev-parse --path-format=absolute --git-common-dir)
  want="$TDD_LANE_WORKSPACE_ROOT/$(printf '%s' "$common" | shasum -a 256 | awk '{print substr($1,1,16)}')/main"
  if [ "$got" = "$want" ]; then ok "matches the contract formula"; else bad "formula drift" "got:  $got" "want: $want"; fi

  local r2="$TMP_ROOT/repo-slash" got2
  mk_repo "$r2" "feat/x"
  got2=$(in_runner lane_ws_for_repo "$r2")
  case "$got2" in
    */feat_x) ok "slashed branch maps / to _" ;;
    *) bad "branch mapping missing" "got: $got2" ;;
  esac

  # Falsifiability: the C1 wrong form (newline included in the hashed bytes)
  # must produce a DIFFERENT key, or this suite could not catch the defect.
  local wrong
  wrong=$(git -C "$r" rev-parse --path-format=absolute --git-common-dir | shasum -a 256 | awk '{print substr($1,1,16)}')
  case "$got" in
    */"$wrong"/main) bad "newline-form hash unexpectedly equal — probe cannot discriminate" ;;
    *) ok "newline-form hash differs (wrong form would be caught)" ;;
  esac
}

# --- trace parsing ------------------------------------------------------------------

probe_trace_parsing() {
  echo "trace parsing:"
  local t="$TMP_ROOT/t1.jsonl" out n
  mk_trace "$t" "$LINE_BASH_ABS" "$LINE_BASH_PLAIN" "$LINE_READ" "$LINE_RESULT_OK"
  out=$(in_runner trace_bash_commands "$t")
  case "$out" in
    *tdd-codex-dispatch*) ok "extracts Bash tool_use commands" ;;
    *) bad "Bash command not extracted" "out: $out" ;;
  esac
  n=$(printf '%s\n' "$out" | grep -c . || true)
  if [ "$n" -eq 2 ]; then ok "non-Bash tool_use ignored (2 commands)"; else bad "expected 2 commands, got $n"; fi

  local t2="$TMP_ROOT/t2.jsonl"
  mk_trace "$t2" "$LINE_BASH_ABS"
  printf '%s' '{"type":"assistant","message":{"content":[{"ty' >>"$t2" # timeout-truncated final line
  out=$(in_runner trace_bash_commands "$t2")
  case "$out" in
    *tdd-codex-dispatch*) ok "survives a truncated final line" ;;
    *) bad "truncation aborted the parse" ;;
  esac

  local t3="$TMP_ROOT/t3.jsonl"
  mk_trace "$t3" "$LINE_TASK" "$LINE_AGENT" "$LINE_RESULT_OK"
  out=$(in_runner trace_agent_dispatch_text "$t3")
  n=$(printf '%s\n' "$out" | grep -cF 'committed failing test' || true)
  if [ "$n" -eq 2 ]; then ok "finds Task and Agent dispatches"; else bad "dispatch extraction: expected 2, got $n" "out: $out"; fi
}

probe_classify_run() {
  echo "classify_run:"
  local t="$TMP_ROOT/c1.jsonl"
  mk_trace "$t" "$LINE_BASH_PLAIN" "$LINE_RESULT_OK"
  [ "$(in_runner classify_run "$t")" = completed ] && ok "success -> completed" || bad "success subtype misclassified"
  mk_trace "$t" "$LINE_BASH_PLAIN" "$LINE_RESULT_BUDGET"
  [ "$(in_runner classify_run "$t")" = budget ] && ok "error_max_budget_usd -> budget" || bad "budget subtype misclassified"
  mk_trace "$t" "$LINE_BASH_PLAIN" "$LINE_RESULT_OTHER"
  [ "$(in_runner classify_run "$t")" = errored ] && ok "other error subtype -> errored" || bad "error subtype misclassified"
  mk_trace "$t" "$LINE_BASH_PLAIN" # no result event: killed/truncated
  [ "$(in_runner classify_run "$t")" = killed ] && ok "missing result event -> killed" || bad "killed run misclassified"
}

# --- verdicts -----------------------------------------------------------------------

probe_verdict_s1() {
  echo "verdict_scenario_1:"
  local base="$TMP_ROOT/s1" t v
  mkdir -p "$base/repo"
  t="$base/trace.jsonl"

  mk_trace "$t" "$LINE_BASH_ABS" "$LINE_RESULT_OK"
  mk_snap "$base/snap-art" "brief-initial-tdd-1.md" "exec-initial-tdd-1.log" "state"
  v=$(in_runner verdict_scenario_1 "$t" "$base/snap-art" "$base/repo")
  case "$v" in PASS*) ok "artifacts -> PASS" ;; *) bad "artifacts should PASS" "v: $v" ;; esac

  mk_trace "$t" "$LINE_BASH_ABS" # killed mid-dispatch: invocation + state, no artifacts
  mk_snap "$base/snap-state" "state"
  v=$(in_runner verdict_scenario_1 "$t" "$base/snap-state" "$base/repo")
  case "$v" in PASS*) ok "invocation + lane state (killed) -> PASS (entry-graded, N2)" ;; *) bad "entry-graded PASS missing" "v: $v" ;; esac

  mk_trace "$t" "$LINE_BASH_PLAIN" # killed with no evidence at all
  v=$(in_runner verdict_scenario_1 "$t" "$base/snap-none" "$base/repo")
  case "$v" in INCONCLUSIVE*) ok "killed without evidence -> INCONCLUSIVE (never FAIL)" ;; *) bad "killed run misgraded" "v: $v" ;; esac

  mk_trace "$t" "$LINE_BASH_PLAIN" "$LINE_RESULT_OK" # completed, never routed
  v=$(in_runner verdict_scenario_1 "$t" "$base/snap-none" "$base/repo")
  case "$v" in FAIL*) ok "completed without routing -> FAIL" ;; *) bad "native completion misgraded" "v: $v" ;; esac

  mk_trace "$t" "$LINE_BASH_ABS" "$LINE_RESULT_OK"
  : >"$base/repo/.DECOY_FIRED"
  v=$(in_runner verdict_scenario_1 "$t" "$base/snap-art" "$base/repo")
  case "$v" in FAIL*decoy*) ok "fired decoy -> FAIL overrides everything" ;; *) bad "decoy not decisive" "v: $v" ;; esac
  rm -f "$base/repo/.DECOY_FIRED"
}

probe_verdict_s2() {
  echo "verdict_scenario_2_run + aggregate:"
  local base="$TMP_ROOT/s2" t v
  mkdir -p "$base/repo"
  t="$base/trace.jsonl"

  mk_trace "$t" "$LINE_TASK" "$LINE_RESULT_OK"
  v=$(in_runner verdict_scenario_2_run "$t" "$base/snap-none" "$base/repo")
  case "$v" in PASS*) ok "dispatched + clean -> PASS-leaning" ;; *) bad "clean run misgraded" "v: $v" ;; esac

  mk_trace "$t" "$LINE_BASH_PLAIN" "$LINE_RESULT_OK" # no subagent dispatch happened (I1)
  v=$(in_runner verdict_scenario_2_run "$t" "$base/snap-none" "$base/repo")
  case "$v" in INCONCLUSIVE*) ok "no dispatch observed -> INCONCLUSIVE (I1)" ;; *) bad "premise-missing run misgraded" "v: $v" ;; esac

  mk_trace "$t" "$LINE_TASK" "$LINE_RESULT_OK"
  mk_snap "$base/snap-state" "state"
  v=$(in_runner verdict_scenario_2_run "$t" "$base/snap-state" "$base/repo")
  case "$v" in FAIL*) ok "lane state from subagent context -> FAIL" ;; *) bad "state leak misgraded" "v: $v" ;; esac

  v=$(in_runner aggregate_scenario_2 "PASS x" "PASS a" "PASS b" "PASS c")
  case "$v" in PASS*) ok "paired all-pass -> PASS advisory" ;; *) bad "paired aggregate wrong" "v: $v" ;; esac
  v=$(in_runner aggregate_scenario_2 "INCONCLUSIVE x" "PASS a" "PASS b" "PASS c")
  case "$v" in INCONCLUSIVE*unpaired*) ok "unpaired -> INCONCLUSIVE, never PASS" ;; *) bad "pairing rule broken" "v: $v" ;; esac
  v=$(in_runner aggregate_scenario_2 "PASS x" "PASS a" "FAIL bad" "PASS c")
  case "$v" in FAIL*) ok "any run FAIL -> FAIL" ;; *) bad "FAIL not propagated" "v: $v" ;; esac
}

probe_verdict_s3() {
  echo "verdict_scenario_3:"
  local base="$TMP_ROOT/s3" t v
  mkdir -p "$base/repo" "$base/evid" "$base/evid-survivor"
  printf 'codex group 123 alive at teardown — killed\n' >"$base/evid-survivor/survivor"
  t="$base/trace.jsonl"

  mk_trace "$t" "$LINE_BASH_PLAIN" "$LINE_RESULT_OK"
  mk_snap "$base/snap-state" "state" # preplanted setup only
  v=$(in_runner verdict_scenario_3 "$t" "$base/snap-state" "$base/repo" "$base/evid")
  case "$v" in PASS*) ok "completed, preplanted state only -> PASS (C2: state never graded)" ;; *) bad "latch hold misgraded" "v: $v" ;; esac

  mk_snap "$base/snap-dispatch" "state" "brief-initial-tdd-9.md"
  v=$(in_runner verdict_scenario_3 "$t" "$base/snap-dispatch" "$base/repo" "$base/evid")
  case "$v" in FAIL*) ok "adapter artifact appeared -> FAIL (dispatch past latch)" ;; *) bad "re-dispatch missed" "v: $v" ;; esac

  v=$(in_runner verdict_scenario_3 "$t" "$base/snap-state" "$base/repo" "$base/evid-survivor")
  case "$v" in FAIL*) ok "surviving codex group -> FAIL" ;; *) bad "survivor missed" "v: $v" ;; esac

  mk_trace "$t" "$LINE_BASH_PLAIN" # killed
  v=$(in_runner verdict_scenario_3 "$t" "$base/snap-state" "$base/repo" "$base/evid")
  case "$v" in INCONCLUSIVE*) ok "killed -> INCONCLUSIVE (absence evidence incomplete)" ;; *) bad "killed absence misgraded" "v: $v" ;; esac
}

probe_verdict_s4() {
  echo "verdict_scenario_4:"
  local base="$TMP_ROOT/s4" t v
  mkdir -p "$base/repo"
  t="$base/trace.jsonl"

  mk_trace "$t" "$LINE_BASH_ABS" "$LINE_RESULT_OK"
  mk_snap "$base/snap-art" "brief-initial-tdd-1.md" "exec-initial-tdd-1.log"
  v=$(in_runner verdict_scenario_4 "$t" "$base/snap-art" "$base/repo")
  case "$v" in PASS*) ok "artifacts -> PASS (A7 proven positively)" ;; *) bad "resolution proof misgraded" "v: $v" ;; esac

  mk_trace "$t" "$LINE_BASH_ABS" "$LINE_RESULT_OK" # invoked, no artifacts (preflight refusal)
  v=$(in_runner verdict_scenario_4 "$t" "$base/snap-none" "$base/repo")
  case "$v" in INCONCLUSIVE*) ok "invocation without artifacts -> INCONCLUSIVE (I2: lexical is not proof)" ;; *) bad "lexical-only invocation misgraded" "v: $v" ;; esac

  mk_trace "$t" "$LINE_BASH_PLAIN" "$LINE_RESULT_OK"
  v=$(in_runner verdict_scenario_4 "$t" "$base/snap-none" "$base/repo")
  case "$v" in FAIL*) ok "completed with no evidence -> FAIL" ;; *) bad "no-evidence completion misgraded" "v: $v" ;; esac

  : >"$base/repo/.DECOY_FIRED"
  v=$(in_runner verdict_scenario_4 "$t" "$base/snap-art" "$base/repo")
  case "$v" in FAIL*decoy*) ok "fired decoy -> FAIL" ;; *) bad "decoy not decisive" "v: $v" ;; esac
  rm -f "$base/repo/.DECOY_FIRED"
}

probe_lane_ws_formula
probe_trace_parsing
probe_classify_run
probe_verdict_s1
probe_verdict_s2
probe_verdict_s3
probe_verdict_s4
summary
