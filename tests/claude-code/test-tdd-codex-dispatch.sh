#!/usr/bin/env bash
# shellcheck disable=SC1010,SC2034,SC2155 # Verbatim Task 7-8 probes use these required forms.
# Hermetic fixture suite for scripts/tdd-codex-dispatch ([local] fork patch #5).
# Self-locating and self-sanitizing (spec §9.2 / ledger A16, A22(2)): pinned
# PATH (the real codex CLI is unreachable), scratch HOME-side state via
# TDD_LANE_WORKSPACE_ROOT + CODEX_HOME, git identity local to each fixture
# repo, no network, no claude. Budget: the whole suite must stay under ~90s —
# it runs at every sync and every promote via patch-5's intent check.
set -uo pipefail # deliberately NOT -e: probes assert on expected failures

SUITE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SUITE_DIR/../.." && pwd)"
ADAPTER="$REPO_ROOT/scripts/tdd-codex-dispatch"

export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_OBJECT_DIRECTORY \
  GIT_LITERAL_PATHSPECS GIT_GLOB_PATHSPECS GIT_NOGLOB_PATHSPECS \
  GIT_ICASE_PATHSPECS GIT_NO_REPLACE_OBJECTS 2>/dev/null || true

TMP_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/tdd-fixtures.XXXXXX")
cleanup() { rm -rf "$TMP_ROOT"; }
trap cleanup EXIT

STUB_BIN="$TMP_ROOT/bin"
mkdir -p "$STUB_BIN"
export PATH="$STUB_BIN:/usr/bin:/bin" # hermetic: system tools only + our stub
export TDD_LANE_WORKSPACE_ROOT="$TMP_ROOT/lane-workspace"
export CODEX_HOME="$TMP_ROOT/codex-home"
export CODEX_STUB_STATE="$TMP_ROOT/stub-state"
mkdir -p "$CODEX_HOME" "$CODEX_STUB_STATE"

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
  printf '\ntest-tdd-codex-dispatch: %d passed, %d failed\n' "$PASS" "$FAIL"
  [ "$FAIL" -eq 0 ]
}

write_auth() { # write_auth MODE — auth.json with the given auth_mode ("" = field absent)
  if [ -n "$1" ]; then
    printf '{"auth_mode": "%s", "tokens": "redacted"}\n' "$1" >"$CODEX_HOME/auth.json"
  else
    printf '{"tokens": "redacted"}\n' >"$CODEX_HOME/auth.json"
  fi
}

write_stub_codex() { # PATH-front fake codex; behavior selected via CODEX_STUB_MODE
  cat >"$STUB_BIN/codex" <<'STUB'
#!/usr/bin/env bash
set -u
if [ "${1:-}" = "--version" ]; then
  printf '%s\n' "${CODEX_STUB_VERSION_OUTPUT:-codex-cli 0.999.0}"
  exit 0
fi
workdir="" reply="" prev=""
for a in "$@"; do
  [ "$prev" = "-C" ] && workdir=$a
  [ "$prev" = "--output-last-message" ] && reply=$a
  prev=$a
done
cat >/dev/null # drain the brief
done_reply() {
  printf 'TDD-DISPATCH-RESULT\nstatus: DONE\nimplemented: impl.txt\nran: make test\nresult: all tests pass\n' >"$reply"
}
case "${CODEX_STUB_MODE:-done}" in
  done) printf 'impl work\n' >>"$workdir/impl.txt"; done_reply ;;
  done-no-evidence)
    printf 'impl work\n' >>"$workdir/impl.txt"
    printf 'TDD-DISPATCH-RESULT\nstatus: DONE\nnothing else to report\n' >"$reply" ;;
  done-tamper)
    printf 'impl work\n' >>"$workdir/impl.txt"
    printf 'hacked\n' >>"$workdir/tests/test_feature.py"; done_reply ;;
  done-tamper-root)
    printf 'impl work\n' >>"$workdir/impl.txt"
    printf 'hacked\n' >>"$workdir/.pre-commit-config.yaml"; done_reply ;;
  done-tamper-docs)
    printf 'impl work\n' >>"$workdir/impl.txt"
    printf 'hacked\n' >>"$workdir/docs/spec.md"; done_reply ;;
  done-veiled-ignore)
    printf 'impl work\n' >>"$workdir/impl.txt"
    mkdir -p "$workdir/sub"
    printf '.gitignore\nconftest.py\n' >"$workdir/sub/.gitignore"
    printf 'evil\n' >"$workdir/sub/conftest.py"; done_reply ;;
  done-untracked-protected)
    printf 'impl work\n' >>"$workdir/impl.txt"
    printf 'planted\n' >"$workdir/tests/test_planted.py"; done_reply ;;
  done-rogue-commit)
    printf 'impl work\n' >>"$workdir/impl.txt"
    git -C "$workdir" add -A >/dev/null 2>&1
    git -C "$workdir" -c user.name=rogue -c user.email=r@x.invalid \
      commit -qm 'rogue: smuggled commit' >/dev/null 2>&1; done_reply ;;
  done-skipflag)
    printf 'impl work\n' >>"$workdir/impl.txt"
    git -C "$workdir" update-index --skip-worktree tests/test_feature.py >/dev/null 2>&1
    done_reply ;;
  blocked-clean) printf 'TDD-DISPATCH-RESULT\nstatus: BLOCKED\nreason: missing schema\n' >"$reply" ;;
  blocked-dirty)
    printf 'leftover experiment\n' >>"$workdir/impl.txt"
    printf 'TDD-DISPATCH-RESULT\nstatus: BLOCKED\nreason: missing schema\n' >"$reply" ;;
  contested)
    printf 'TDD-DISPATCH-RESULT\nstatus: TEST_CONTESTED\ntest: tests/test_feature.py::test_feature\nclaim: expected value inverted vs the task statement\nexpected: feature() == 2\n' >"$reply" ;;
  protected-change)
    printf 'TDD-DISPATCH-RESULT\nstatus: PROTECTED_CHANGE_REQUESTED\npath: package.json\nrationale: implementation requires the left-pad dependency\nchange: add "left-pad": "^1.3.0" to dependencies\n' >"$reply" ;;
  malformed-none) printf 'I did things but forgot the block entirely.\n' >"$reply" ;;
  malformed-two) printf 'TDD-DISPATCH-RESULT\nstatus: DONE\nand again:\nTDD-DISPATCH-RESULT\nstatus: DONE\n' >"$reply" ;;
  malformed-badline) printf 'TDD-DISPATCH-RESULT\nStatus DONE-ish, kind of\n' >"$reply" ;;
  sleep-forever) printf 'never finished\n' >"$reply"; exec sleep 600 ;;
  spawn-child)
    sleep 600 &
    printf '%s\n' "$!" >"$CODEX_STUB_STATE/child.pid"
    exec sleep 600 ;;
  exit-nonzero) printf 'boom\n' >"$reply"; exit 17 ;;
esac
exit 0
STUB
  chmod +x "$STUB_BIN/codex"
}

mk_repo() { # mk_repo DIR — repo on branch main, HEAD = the RED-commit-shaped base
  local dir=$1
  mkdir -p "$dir/tests"
  git init -q "$dir"
  git -C "$dir" checkout -q -b main 2>/dev/null || true
  git -C "$dir" config user.name fixture
  git -C "$dir" config user.email fixture@example.invalid
  printf 'def test_feature():\n    assert feature() == 1\n' >"$dir/tests/test_feature.py"
  printf 'repos: []\n' >"$dir/.pre-commit-config.yaml"
  printf '{"name": "fixture"}\n' >"$dir/package.json"
  git -C "$dir" add -A
  git -C "$dir" commit -qm 'test: red suite (fixture base)'
}

lane_workspace_for() { # lane_workspace_for WORKTREE BRANCH — MUST match the adapter's formula
  local common key bkey
  common=$(git -C "$1" rev-parse --path-format=absolute --git-common-dir)
  key=$(printf '%s' "$common" | shasum -a 256 | awk '{print substr($1, 1, 16)}')
  bkey=$(printf '%s' "$2" | tr '/' '_')
  printf '%s/%s/%s' "$TDD_LANE_WORKSPACE_ROOT" "$key" "$bkey"
}

write_state() { # write_state WORKTREE BRANCH BASE_SHA [ESCALATED] — echoes the workspace dir
  local ws
  ws=$(lane_workspace_for "$1" "$2")
  mkdir -p "$ws"
  {
    printf 'repo=%s\n' "$(git -C "$1" rev-parse --path-format=absolute --git-common-dir)"
    printf 'branch=%s\n' "$2"
    printf 'phase=red-committed\n'
    printf 'base_sha=%s\n' "$3"
    printf 'fix_rounds_used=0\n'
    printf 'escalated=%s\n' "${4:-false}"
  } >"$ws/state"
  printf '%s' "$ws"
}

new_lane() { # new_lane NAME [ESCALATED] — sets REPO, BASE, WS globals for a fresh lane
  REPO="$TMP_ROOT/repo-$1"
  mk_repo "$REPO"
  BASE=$(git -C "$REPO" rev-parse HEAD)
  WS=$(write_state "$REPO" main "$BASE" "${2:-false}")
}

run_adapter() { # run_adapter WORKTREE [extra args...] — brief on stdin; out/err captured
  local wt=$1
  shift
  printf 'Task: make the committed failing tests green.\n' \
    | "$ADAPTER" --worktree "$wt" --branch main \
      --commit-subject 'feat: implement feature' --effort medium "$@" \
      >"$TMP_ROOT/out" 2>"$TMP_ROOT/err"
}

result_file_in() { # result_file_in WS — path of the newest result record, if any
  ls -t "$1"/result-* 2>/dev/null | awk 'NR==1'
}

# --- probes ------------------------------------------------------------------
write_stub_codex
write_auth chatgpt

probe_preflight_quartet() { # spec §9.2 — A15: each exits 2, diagnostic, NO record
  local rc rf
  # 1: missing codex binary
  new_lane a15-missing
  rm -f "$STUB_BIN/codex"
  run_adapter "$REPO"; rc=$?
  rf=$(result_file_in "$WS")
  if [ "$rc" -eq 2 ] && grep -q 'codex CLI is not on PATH' "$TMP_ROOT/err" && [ -z "$rf" ] \
    && ! grep -q 'TDD-DISPATCH-RESULT' "$TMP_ROOT/out"; then
    ok 'A15: missing codex binary -> exit 2, stderr diagnostic, no record'
  else
    bad 'A15: missing codex binary' "rc=$rc" "$(cat "$TMP_ROOT/err")"
  fi
  write_stub_codex
  # 2: unparseable --version
  new_lane a15-badver
  CODEX_STUB_VERSION_OUTPUT='codex nightly build (unversioned)' run_adapter "$REPO"; rc=$?
  if [ "$rc" -eq 2 ] && grep -q "could not parse 'codex --version'" "$TMP_ROOT/err" \
    && [ -z "$(result_file_in "$WS")" ]; then
    ok 'A15: unparseable codex --version -> exit 2, no record'
  else
    bad 'A15: unparseable codex --version' "rc=$rc" "$(cat "$TMP_ROOT/err")"
  fi
  # 3: auth.json without auth_mode
  new_lane a15-noauthmode
  write_auth ""
  run_adapter "$REPO"; rc=$?
  if [ "$rc" -eq 2 ] && grep -q 'no auth_mode' "$TMP_ROOT/err" \
    && [ -z "$(result_file_in "$WS")" ]; then
    ok 'A15: auth.json without auth_mode -> exit 2, no record'
  else
    bad 'A15: auth.json without auth_mode' "rc=$rc" "$(cat "$TMP_ROOT/err")"
  fi
  # 4: wrong auth_mode for the chosen billing lane
  new_lane a15-wrongmode
  write_auth apikey
  run_adapter "$REPO"; rc=$?
  if [ "$rc" -eq 2 ] && grep -q 'does not match the chosen billing lane' "$TMP_ROOT/err" \
    && [ -z "$(result_file_in "$WS")" ]; then
    ok 'A15: wrong auth_mode -> exit 2, no record'
  else
    bad 'A15: wrong auth_mode' "rc=$rc" "$(cat "$TMP_ROOT/err")"
  fi
  write_auth chatgpt
}

probe_preflight_repo() {
  local rc
  # detached HEAD refused (A5)
  new_lane pf-detached
  git -C "$REPO" checkout -q --detach
  run_adapter "$REPO"; rc=$?
  if [ "$rc" -eq 2 ] && grep -q 'detached HEAD' "$TMP_ROOT/err"; then
    ok 'A5: detached HEAD refused at preflight'
  else
    bad 'A5: detached HEAD refused' "rc=$rc" "$(cat "$TMP_ROOT/err")"
  fi
  # dirty worktree refused
  new_lane pf-dirty
  printf 'wip\n' >>"$REPO/impl.txt"
  run_adapter "$REPO"; rc=$?
  if [ "$rc" -eq 2 ] && grep -q 'not clean at dispatch' "$TMP_ROOT/err"; then
    ok 'preflight: dirty worktree refused'
  else
    bad 'preflight: dirty worktree refused' "rc=$rc"
  fi
  # non-root worktree path refused
  new_lane pf-subdir
  mkdir -p "$REPO/sub"
  run_adapter "$REPO/sub"; rc=$?
  if [ "$rc" -eq 2 ]; then
    ok 'preflight: non-root worktree path refused'
  else
    bad 'preflight: non-root worktree path refused' "rc=$rc"
  fi
  # bad conventional subject refused (#17 residue 1)
  new_lane pf-subject
  printf 'brief\n' | "$ADAPTER" --worktree "$REPO" --branch main \
    --commit-subject 'implement the feature' --effort medium \
    >"$TMP_ROOT/out" 2>"$TMP_ROOT/err"; rc=$?
  if [ "$rc" -eq 2 ] && grep -q 'not a conventional subject' "$TMP_ROOT/err"; then
    ok '#17r1: non-conventional --commit-subject refused'
  else
    bad '#17r1: non-conventional --commit-subject refused' "rc=$rc"
  fi
  # bad effort refused
  new_lane pf-effort
  printf 'brief\n' | "$ADAPTER" --worktree "$REPO" --branch main \
    --commit-subject 'feat: x' --effort xhigh \
    >"$TMP_ROOT/out" 2>"$TMP_ROOT/err"; rc=$?
  if [ "$rc" -eq 2 ] && grep -q -- '--effort must be medium or high' "$TMP_ROOT/err"; then
    ok 'preflight: effort whitelist enforced'
  else
    bad 'preflight: effort whitelist enforced' "rc=$rc"
  fi
}

probe_protect_flags() { # A8/A23(4): typed, magic-rejecting, fail-closed
  local rc bad_value
  new_lane protect
  for bad_value in ':(exclude)tests/**' '/etc/passwd' '../outside' 'a/../../b' ''; do
    run_adapter "$REPO" --protect-path "$bad_value"; rc=$?
    if [ "$rc" -eq 2 ]; then
      ok "A8: --protect-path rejects '$bad_value'"
    else
      bad "A8: --protect-path rejects '$bad_value'" "rc=$rc"
    fi
  done
  run_adapter "$REPO" --protect-glob ':(top)docs/**'; rc=$?
  if [ "$rc" -eq 2 ]; then
    ok 'A8: --protect-glob rejects pathspec magic'
  else
    bad 'A8: --protect-glob rejects pathspec magic' "rc=$rc"
  fi
  # union-only extension actually protects: Sol touches docs/spec.md under --protect-glob
  new_lane protect-union
  printf 'spec\n' >"$REPO/docs-spec.md" 2>/dev/null || true
  mkdir -p "$REPO/docs"
  printf 'spec\n' >"$REPO/docs/spec.md"
  git -C "$REPO" add -A && git -C "$REPO" commit -qm 'test: add spec doc'
  BASE=$(git -C "$REPO" rev-parse HEAD)
  WS=$(write_state "$REPO" main "$BASE")
  CODEX_STUB_MODE=done-tamper-docs run_adapter "$REPO" --protect-glob 'docs/**'; rc=$?
  if [ "$rc" -eq 4 ] && grep -q 'docs/spec.md' "$TMP_ROOT/err"; then
    ok 'A8: --protect-glob extension is enforced (union with floor)'
  else
    bad 'A8: --protect-glob extension enforced' "rc=$rc" "$(cat "$TMP_ROOT/err")"
  fi
  # veiled-ignore closure (floor-bypass class): a pre-existing untracked
  # .gitignore anywhere refuses at preflight
  new_lane protect-veiled-pre
  mkdir -p "$REPO/sub"
  printf '.gitignore\nconftest.py\n' >"$REPO/sub/.gitignore"
  run_adapter "$REPO"; rc=$?
  if [ "$rc" -eq 2 ] && grep -q 'would veil the protection floor' "$TMP_ROOT/err"; then
    ok 'floor: untracked nested .gitignore refused at preflight'
  else
    bad 'floor: untracked nested .gitignore refused at preflight' "rc=$rc" "$(cat "$TMP_ROOT/err")"
  fi
  # Sol-planted self-ignoring .gitignore is caught post-run despite the veil
  new_lane protect-veiled-post
  CODEX_STUB_MODE=done-veiled-ignore run_adapter "$REPO"; rc=$?
  if [ "$rc" -eq 4 ] && grep -q 'sub/.gitignore' "$TMP_ROOT/err"; then
    ok 'floor: Sol-planted self-ignoring .gitignore caught post-run'
  else
    bad 'floor: Sol-planted self-ignoring .gitignore caught post-run' "rc=$rc" "$(cat "$TMP_ROOT/err")"
  fi
}

probe_state_gates() { # A2/A18/A19
  local rc
  # missing state file
  new_lane state-missing
  rm -f "$WS/state"
  run_adapter "$REPO"; rc=$?
  if [ "$rc" -eq 2 ] && grep -q 'no lane state file' "$TMP_ROOT/err"; then
    ok 'A2: missing state file refused'
  else
    bad 'A2: missing state file refused' "rc=$rc"
  fi
  # escalated latch (A18): fresh Sol dispatch forbidden
  new_lane state-escalated true
  run_adapter "$REPO"; rc=$?
  if [ "$rc" -eq 2 ] && grep -q 'escalated' "$TMP_ROOT/err"; then
    ok 'A18: escalated latch forbids Sol dispatch'
  else
    bad 'A18: escalated latch forbids Sol dispatch' "rc=$rc"
  fi
  # base mismatch (stale state)
  new_lane state-stale
  printf 'wip\n' >>"$REPO/impl.txt"
  git -C "$REPO" add -A && git -C "$REPO" commit -qm 'feat: drift past base'
  run_adapter "$REPO"; rc=$?
  if [ "$rc" -eq 2 ] && grep -q 're-freeze base' "$TMP_ROOT/err"; then
    ok 'A19: HEAD != state base_sha refused'
  else
    bad 'A19: HEAD != state base_sha refused' "rc=$rc"
  fi
  # branch mismatch
  new_lane state-branch
  sed -e 's/^branch=main/branch=other/' "$WS/state" >"$WS/state.tmp" && mv "$WS/state.tmp" "$WS/state"
  run_adapter "$REPO"; rc=$?
  if [ "$rc" -eq 2 ] && grep -q 'one lane per repo+branch' "$TMP_ROOT/err"; then
    ok 'A19: state/branch mismatch refused'
  else
    bad 'A19: state/branch mismatch refused' "rc=$rc"
  fi
}

wait_for_file() { # wait_for_file PATH TRIES — poll at 0.2s-ish granularity via sleep 1 fallback
  local i
  for i in $(seq 1 "$2"); do
    [ -e "$1" ] && return 0
    sleep 1
  done
  return 1
}

probe_survivability() {
  local rc rf child adapter_pid lockd real_pid
  # internal timeout: group killed, CANCELLED record, exit 3 (A13/A20)
  new_lane surv-timeout
  CODEX_STUB_MODE=sleep-forever run_adapter "$REPO" --timeout-seconds 6; rc=$?
  rf=$(result_file_in "$WS")
  if [ "$rc" -eq 3 ] && [ -n "$rf" ] && grep -q '^status: CANCELLED$' "$rf" \
    && grep -q 'wall-clock cap' "$rf"; then
    ok 'A20: internal timeout -> exit 3 + atomic CANCELLED record'
  else
    bad 'A20: internal timeout' "rc=$rc" "record: ${rf:-none}"
  fi
  # codex nonzero exit: CANCELLED record, exit 3
  new_lane surv-codexfail
  CODEX_STUB_MODE=exit-nonzero run_adapter "$REPO"; rc=$?
  rf=$(result_file_in "$WS")
  if [ "$rc" -eq 3 ] && [ -n "$rf" ] && grep -q 'codex exec exited 17' "$rf"; then
    ok 'A20: codex nonzero exit -> exit 3 + CANCELLED record'
  else
    bad 'A20: codex nonzero exit' "rc=$rc"
  fi
  # TERM mid-dispatch (= harness task-stop): whole group dies, record written,
  # lock released, adapter survived its own group kill (macOS set -m check)
  local sig
  for sig in TERM INT; do
    new_lane "surv-$sig"
    rm -f "$CODEX_STUB_STATE/child.pid"
    ( CODEX_STUB_MODE=spawn-child run_adapter "$REPO" --timeout-seconds 120 ) &
    adapter_pid=$!
    if ! wait_for_file "$CODEX_STUB_STATE/child.pid" 20; then
      bad "A13: $sig mid-dispatch — stub child never appeared"
      kill -KILL "$adapter_pid" 2>/dev/null || true
      continue
    fi
    child=$(cat "$CODEX_STUB_STATE/child.pid")
    sleep 1
    # Signal the ADAPTER — its pid is in the lock. Killing the wrapping
    # subshell instead would prove nothing: bash does not forward signals
    # to children, so the adapter would sail on, orphaned.
    lockd="$(git -C "$REPO" rev-parse --path-format=absolute --git-dir)/tdd-dispatch.lock"
    real_pid=$(cat "$lockd/pid" 2>/dev/null || true)
    if [ -z "$real_pid" ]; then
      bad "A13: $sig mid-dispatch — no lock-recorded adapter pid"
      kill -KILL "$child" 2>/dev/null || true
      continue
    fi
    kill "-$sig" "$real_pid" 2>/dev/null
    wait "$adapter_pid" 2>/dev/null; rc=$?
    sleep 1
    rf=$(result_file_in "$WS")
    if [ "$rc" -eq 3 ] && [ -n "$rf" ] && grep -q '^status: CANCELLED$' "$rf" \
      && ! kill -0 "$child" 2>/dev/null \
      && [ ! -d "$(git -C "$REPO" rev-parse --path-format=absolute --git-dir)/tdd-dispatch.lock" ]; then
      ok "A13/A20: $sig mid-dispatch -> group dead, CANCELLED record, lock released, exit 3"
    else
      bad "A13/A20: $sig mid-dispatch" "rc=$rc" "record=${rf:-none}" \
        "child alive: $(kill -0 "$child" 2>/dev/null && echo yes || echo no)"
      kill -KILL "$child" 2>/dev/null || true
    fi
  done
  # SIGKILL: no record (unavoidable), lock held fail-closed (A20(2)/A13(3))
  new_lane surv-kill
  rm -f "$CODEX_STUB_STATE/child.pid"
  ( CODEX_STUB_MODE=spawn-child run_adapter "$REPO" --timeout-seconds 120 ) &
  adapter_pid=$!
  wait_for_file "$CODEX_STUB_STATE/child.pid" 20 || true
  child=$(cat "$CODEX_STUB_STATE/child.pid" 2>/dev/null || true)
  # the subshell wrapping run_adapter shields the adapter pid — find it via the lock
  lockd="$(git -C "$REPO" rev-parse --path-format=absolute --git-dir)/tdd-dispatch.lock"
  real_pid=$(cat "$lockd/pid" 2>/dev/null || true)
  if [ -n "$real_pid" ]; then
    kill -KILL "$real_pid" 2>/dev/null
    sleep 1
    rf=$(result_file_in "$WS")
    if [ -z "$rf" ] && [ -d "$lockd" ]; then
      ok 'A20(2): SIGKILL -> no record, lock held fail-closed'
    else
      bad 'A20(2): SIGKILL residue' "record=${rf:-none}" "lock present: $([ -d "$lockd" ] && echo yes || echo no)"
    fi
    # stale-reclaim: the CODEX group (lock pgid = stub's group, NOT the
    # suite's) is still alive after the adapter's SIGKILL — a fresh dispatch
    # must REFUSE; after the probe kills that group it must RECLAIM
    local held_pgid; held_pgid=$(cat "$lockd/pgid" 2>/dev/null || true)
    CODEX_STUB_MODE=blocked-clean run_adapter "$REPO"; rc=$?
    if [ "$rc" -eq 2 ] && grep -q 'one lane at a time' "$TMP_ROOT/err"; then
      ok 'A13(3): live process group -> lock refusal (no false reclaim)'
    else
      bad 'A13(3): live-group lock refusal' "rc=$rc"
    fi
    if [ -n "$held_pgid" ]; then kill -KILL -- "-$held_pgid" 2>/dev/null || true; fi
    kill -KILL "$child" 2>/dev/null || true
    sleep 1
    CODEX_STUB_MODE=blocked-clean run_adapter "$REPO"; rc=$?
    if [ "$rc" -eq 0 ]; then
      ok 'A13(3): empty process group -> stale lock reclaimed'
    else
      bad 'A13(3): stale lock reclaim' "rc=$rc" "$(cat "$TMP_ROOT/err")"
    fi
  else
    bad 'A20(2): SIGKILL probe could not find the adapter pid via the lock'
    kill -KILL "$adapter_pid" "$child" 2>/dev/null || true
  fi
  wait "$adapter_pid" 2>/dev/null || true
  # atomic-record contract (A20 torn-write): tmp+rename shape asserted at source
  if grep -qF 'local tmp="$result_file.tmp.$$"' "$ADAPTER" \
    && grep -qF 'mv "$tmp" "$result_file"' "$ADAPTER"; then
    ok 'A20: result records are written via temp-file + rename (torn-write contract)'
  else
    bad 'A20: atomic-record contract missing from adapter source'
  fi
}

probe_lock() { # second dispatch refused while the first holds the lane
  local rc adapter_pid
  new_lane lock-contention
  rm -f "$CODEX_STUB_STATE/child.pid"
  ( CODEX_STUB_MODE=spawn-child run_adapter "$REPO" --timeout-seconds 120 ) &
  adapter_pid=$!
  wait_for_file "$CODEX_STUB_STATE/child.pid" 20 || true
  CODEX_STUB_MODE=blocked-clean run_adapter "$REPO"; rc=$?
  if [ "$rc" -eq 2 ] && grep -q 'one lane at a time' "$TMP_ROOT/err"; then
    ok 'lock: concurrent dispatch refused while lane is live'
  else
    bad 'lock: concurrent dispatch refused' "rc=$rc"
  fi
  local lockd="$(git -C "$REPO" rev-parse --path-format=absolute --git-dir)/tdd-dispatch.lock"
  local real_pid; real_pid=$(cat "$lockd/pid" 2>/dev/null || true)
  [ -n "$real_pid" ] && kill -TERM "$real_pid" 2>/dev/null
  wait "$adapter_pid" 2>/dev/null || true
  local child; child=$(cat "$CODEX_STUB_STATE/child.pid" 2>/dev/null || true)
  [ -n "$child" ] && kill -KILL "$child" 2>/dev/null || true
}

probe_happy_path() {
  local rc rf head parents
  new_lane happy
  CODEX_STUB_MODE=done run_adapter "$REPO"; rc=$?
  rf=$(result_file_in "$WS")
  head=$(git -C "$REPO" rev-parse HEAD)
  parents=$(git -C "$REPO" rev-list --parents -n1 HEAD)
  if [ "$rc" -eq 0 ] \
    && [ -n "$rf" ] \
    && [ "$(sed -n '1p' "$rf")" = "TDD-DISPATCH-RESULT" ] \
    && [ "$(sed -n '2p' "$rf")" = "status: DONE" ] \
    && [ "$(printf '%s' "$parents" | wc -w | tr -d ' ')" = "2" ] \
    && [ "$(printf '%s' "$parents" | awk '{print $2}')" = "$BASE" ] \
    && [ -z "$(git -C "$REPO" status --porcelain)" ] \
    && git -C "$REPO" show -s --format=%B HEAD | grep -qxF 'Agent: implementer (GPT 5.6 Sol)' \
    && git -C "$REPO" show -s --format=%s HEAD | grep -qxF 'feat: implement feature' \
    && [ -s "$WS/g4-diff-$(sed -n 's/^dispatch: //p' "$rf").patch" ] \
    && grep -qx 'impl.txt' "$WS/g4-changed-files-$(sed -n 's/^dispatch: //p' "$rf").txt"; then
    ok 'happy path: DONE -> trusted commit on base, record + G4 artifacts, clean tree'
  else
    bad 'happy path' "rc=$rc" "record=${rf:-none}" "parents=$parents"
  fi
  # env-immunity (A8): hostile pathspec env must not alter G2/staging semantics
  new_lane happy-env
  GIT_LITERAL_PATHSPECS=1 GIT_GLOB_PATHSPECS=1 CODEX_STUB_MODE=done run_adapter "$REPO"; rc=$?
  if [ "$rc" -eq 0 ]; then
    ok 'A8: adapter git calls immune to inherited GIT_*_PATHSPECS'
  else
    bad 'A8: pathspec-env immunity' "rc=$rc" "$(cat "$TMP_ROOT/err")"
  fi
  # evidence gate: DONE without test evidence is refused BEFORE any commit lands
  new_lane happy-noevidence
  CODEX_STUB_MODE=done-no-evidence run_adapter "$REPO"; rc=$?
  if [ "$rc" -eq 4 ] && grep -q 'no test' "$TMP_ROOT/err" \
    && [ "$(git -C "$REPO" rev-parse HEAD)" = "$BASE" ]; then
    ok 'evidence gate: DONE with no test evidence -> exit 4, repo still at base'
  else
    bad 'evidence gate' "rc=$rc" "head=$(git -C "$REPO" rev-parse HEAD) base=$BASE"
  fi
}

probe_g2_and_protection() {
  local rc
  # nested protected tamper (tests/**)
  new_lane g2-nested
  CODEX_STUB_MODE=done-tamper run_adapter "$REPO"; rc=$?
  if [ "$rc" -eq 4 ] && grep -q 'tests/test_feature.py' "$TMP_ROOT/err"; then
    ok 'G2: nested protected tamper caught, path named, exit 4'
  else
    bad 'G2: nested protected tamper' "rc=$rc"
  fi
  # ROOT protected file tamper — the A8 root-miss regression probe
  new_lane g2-root
  CODEX_STUB_MODE=done-tamper-root run_adapter "$REPO"; rc=$?
  if [ "$rc" -eq 4 ] && grep -q '.pre-commit-config.yaml' "$TMP_ROOT/err"; then
    ok 'A8: root-level protected file tamper caught (:(literal,top) reaches the root)'
  else
    bad 'A8: root-level tamper' "rc=$rc" "$(cat "$TMP_ROOT/err")"
  fi
  # untracked file planted under a protected tree — the fail-early worktree twin
  new_lane g2-untracked
  CODEX_STUB_MODE=done-untracked-protected run_adapter "$REPO"; rc=$?
  if [ "$rc" -eq 4 ] && grep -q 'tests/test_planted.py' "$TMP_ROOT/err"; then
    ok 'G2 twin: untracked file under protected tree caught before staging'
  else
    bad 'G2 twin: untracked protected plant' "rc=$rc"
  fi
  # rogue commit smuggled during dispatch -> parent != base (A5)
  new_lane g2-rogue
  CODEX_STUB_MODE=done-rogue-commit run_adapter "$REPO"; rc=$?
  if [ "$rc" -eq 4 ] && grep -q 'not base' "$TMP_ROOT/err"; then
    ok 'A5: smuggled mid-dispatch commit -> parent!=base, exit 4'
  else
    bad 'A5: rogue commit' "rc=$rc" "$(cat "$TMP_ROOT/err")"
  fi
  # skip-worktree flag planted -> A12 index-flag assertion
  new_lane g2-skipflag
  CODEX_STUB_MODE=done-skipflag run_adapter "$REPO"; rc=$?
  if [ "$rc" -eq 4 ] && grep -qi 'skip-worktree' "$TMP_ROOT/err"; then
    ok 'A12: skip-worktree flag on a protected path -> exit 4'
  else
    bad 'A12: index-flag assertion' "rc=$rc" "$(cat "$TMP_ROOT/err")"
  fi
}

probe_non_done() {
  local rc rf
  # clean BLOCKED accepted: record written, no commit, state preserved
  new_lane nd-clean
  CODEX_STUB_MODE=blocked-clean run_adapter "$REPO"; rc=$?
  rf=$(result_file_in "$WS")
  if [ "$rc" -eq 0 ] && [ -n "$rf" ] && grep -q '^status: BLOCKED$' "$rf" \
    && [ "$(git -C "$REPO" rev-parse HEAD)" = "$BASE" ]; then
    ok 'A4: clean BLOCKED -> exit 0, record, repo untouched'
  else
    bad 'A4: clean BLOCKED' "rc=$rc"
  fi
  # dirty BLOCKED refused: A4 byte-equal violated -> exit 4, NO record
  new_lane nd-dirty
  CODEX_STUB_MODE=blocked-dirty run_adapter "$REPO"; rc=$?
  rf=$(result_file_in "$WS")
  if [ "$rc" -eq 4 ] && [ -z "$rf" ] && grep -q 'differs from dispatch capture' "$TMP_ROOT/err"; then
    ok 'A4: dirty BLOCKED -> exit 4, status NOT accepted, no record'
  else
    bad 'A4: dirty BLOCKED' "rc=$rc" "record=${rf:-none}"
  fi
  # TEST_CONTESTED and PROTECTED_CHANGE_REQUESTED pass through clean
  local mode want
  for mode in contested protected-change; do
    case "$mode" in
      contested) want='TEST_CONTESTED' ;;
      *) want='PROTECTED_CHANGE_REQUESTED' ;;
    esac
    new_lane "nd-$mode"
    CODEX_STUB_MODE=$mode run_adapter "$REPO"; rc=$?
    rf=$(result_file_in "$WS")
    if [ "$rc" -eq 0 ] && [ -n "$rf" ] && grep -q "^status: $want$" "$rf" \
      && [ "$(git -C "$REPO" rev-parse HEAD)" = "$BASE" ]; then
      ok "G5: $want -> exit 0, record, repo untouched"
    else
      bad "G5: $want" "rc=$rc"
    fi
  done
}

probe_malformed() { # A14(5): 0 / 2 headers, bad status line -> synthesized MALFORMED
  local mode rc rf
  for mode in malformed-none malformed-two malformed-badline; do
    new_lane "mf-$mode"
    CODEX_STUB_MODE=$mode run_adapter "$REPO"; rc=$?
    rf=$(result_file_in "$WS")
    if [ "$rc" -eq 0 ] && [ -n "$rf" ] && grep -q '^status: MALFORMED$' "$rf" \
      && [ "$(git -C "$REPO" rev-parse HEAD)" = "$BASE" ] \
      && [ -z "$(git -C "$REPO" status --porcelain)" ]; then
      ok "A14(5): $mode -> exit 0, synthesized MALFORMED record, repo untouched"
    else
      bad "A14(5): $mode" "rc=$rc" "record=${rf:-none}"
    fi
  done
}

probe_fix_round() {
  local rc first_sha second_sha parents
  new_lane fixround
  CODEX_STUB_MODE=done run_adapter "$REPO"; rc=$?
  if [ "$rc" -ne 0 ]; then bad 'fix round: setup DONE failed' "rc=$rc"; return; fi
  first_sha=$(git -C "$REPO" rev-parse HEAD)
  # controller re-dispatches a fix round: state base unchanged, HEAD = Sol's commit
  CODEX_STUB_MODE=done run_adapter "$REPO" --fix-round 1; rc=$?
  second_sha=$(git -C "$REPO" rev-parse HEAD)
  parents=$(git -C "$REPO" rev-list --parents -n1 HEAD)
  if [ "$rc" -eq 0 ] && [ "$second_sha" != "$first_sha" ] \
    && [ "$(printf '%s' "$parents" | awk '{print $2}')" = "$BASE" ] \
    && [ "$(git -C "$REPO" rev-list --count "$BASE..HEAD")" = "1" ]; then
    ok 'A1/A5: fix round amends — SHA changed, sole parent still base, one impl commit'
  else
    bad 'A1/A5: fix round amend' "rc=$rc" "first=$first_sha second=$second_sha"
  fi
  # fix round against a bare base (no impl commit) is refused at preflight
  new_lane fixround-bare
  CODEX_STUB_MODE=done run_adapter "$REPO" --fix-round 1; rc=$?
  if [ "$rc" -eq 2 ] && grep -q 'sole parent must be base' "$TMP_ROOT/err"; then
    ok 'A5: fix round without an implementation commit refused'
  else
    bad 'A5: bare fix round refused' "rc=$rc"
  fi
}

probe_git_semantics() { # A12/A23(1): disposable-checkout GREEN facts the lane doc relies on
  local repo="$TMP_ROOT/semantics"
  mk_repo "$repo"
  printf 'deps/\n' >"$repo/.gitignore"
  mkdir -p "$repo/deps"
  printf 'dep blob\n' >"$repo/deps/lib.txt"
  git -C "$repo" add .gitignore && git -C "$repo" commit -qm 'test: ignore deps'
  local co="$TMP_ROOT/semantics-co"
  git -C "$repo" worktree add --detach "$co" HEAD >/dev/null 2>&1
  if [ ! -e "$co/deps/lib.txt" ]; then
    ok 'A12: detached checkout excludes ignored dirs (contamination fixture)'
  else
    bad 'A12: detached checkout contamination — ignored dir present without symlink'
  fi
  ln -s "$repo/deps" "$co/deps"
  if [ -e "$co/deps/lib.txt" ]; then
    ok 'A12: explicit dep symlink shares the dependency dir (accepted channel)'
  else
    bad 'A12: dep symlink sharing failed'
  fi
  git -C "$repo" worktree remove --force "$co" >/dev/null 2>&1 || true
}

probe_verdict_grammar() { # the lane doc's documented parse (guard + awk) vs 6 samples
  local parse f
  parse() {
    [ "$(grep -cx 'TDD-G4-VERDICT' "$1")" -eq 1 ] &&
    awk '/^TDD-G4-VERDICT$/ { if ((getline l) <= 0) exit 1;
      if (l ~ /^verdict: (PASS|FINDINGS|PROBE_REQUEST)$/) { sub(/^verdict: /, "", l); print l; exit 0 }
      exit 1 }' "$1"
  }
  f="$TMP_ROOT/verdict"
  printf 'prose...\nTDD-G4-VERDICT\nverdict: PASS\n' >"$f"
  [ "$(parse "$f")" = "PASS" ] && ok 'A17: verdict parse — PASS' || bad 'A17: verdict parse — PASS'
  printf 'TDD-G4-VERDICT\nverdict: FINDINGS\nfinding: src/x.py:10 — lookup table mirrors test constants\n' >"$f"
  [ "$(parse "$f")" = "FINDINGS" ] && ok 'A17: verdict parse — FINDINGS' || bad 'A17: verdict parse — FINDINGS'
  printf 'TDD-G4-VERDICT\nverdict: PROBE_REQUEST\nprobe: pytest -k prop :: passes :: property sweep\n' >"$f"
  [ "$(parse "$f")" = "PROBE_REQUEST" ] && ok 'A17: verdict parse — PROBE_REQUEST' || bad 'A17: verdict parse — PROBE_REQUEST'
  printf 'TDD-G4-VERDICT\nI think it is fine\n' >"$f"
  if [ -z "$(parse "$f")" ]; then
    ok 'A17: verdict parse — malformed fails closed'
  else
    bad 'A17: verdict parse — malformed fails closed'
  fi
  printf 'quoted:\nTDD-G4-VERDICT\nverdict: PASS\nreal:\nTDD-G4-VERDICT\nverdict: FINDINGS\n' >"$f"
  if [ -z "$(parse "$f")" ]; then
    ok 'A17: verdict parse — duplicate header fails closed'
  else
    bad 'A17: verdict parse — duplicate header fails closed'
  fi
  printf 'no verdict block at all\n' >"$f"
  if [ -z "$(parse "$f")" ]; then
    ok 'A17: verdict parse — missing header fails closed'
  else
    bad 'A17: verdict parse — missing header fails closed'
  fi
}

probe_preflight_quartet
probe_preflight_repo
probe_protect_flags
probe_state_gates
probe_survivability
probe_lock
probe_happy_path
probe_g2_and_protection
probe_non_done
probe_malformed
probe_fix_round
probe_git_semantics
probe_verdict_grammar
summary
