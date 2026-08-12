#!/usr/bin/env bash
#
# update-hop.sh — local delivery hop for fork releases (RESEARCH.md §4,
# finding #5): refresh the superpowers-dev marketplace, update the
# installed plugin when the fork has cut a new version, and assert the
# delivery actually landed.
#
# Verification surface: the plugin CACHE's plugin.json version — the only
# field refreshed on update. The install-registry's commit-SHA field is
# stale by design and is never consulted here.
#
# Designed to run unattended (daily launchd job) and to be safe to run
# any time: desired-state, idempotent, fail-closed. Running Claude
# sessions are unaffected by an update (version-dir insulated); new
# sessions pick up the new version. Network-touching CLI calls get a
# bounded retry so a transient blip doesn't defer delivery a full day.
set -euo pipefail

MARKETPLACE="superpowers-dev"
PLUGIN="superpowers@superpowers-dev"
MARKETPLACE_MANIFEST="$HOME/.claude/plugins/marketplaces/$MARKETPLACE/.claude-plugin/marketplace.json"
CACHE_ROOT="$HOME/.claude/plugins/cache/$MARKETPLACE/superpowers"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

# Bounded retry for the network-touching CLI calls: a transient GitHub blip
# should not cost a day of delivery latency (the next launchd fire). The
# native CLI restores marketplace state on a failed re-clone (see the
# 2026-08-05 gotcha), so repeating an attempt is safe; exhaustion stays
# fail-closed — the non-zero return exits the script under set -e.
RETRY_ATTEMPTS=3
RETRY_DELAYS=(20 40)  # seconds before attempt 2, attempt 3

retry_cli() {
  local label="$1"; shift
  local attempt delay
  for (( attempt=1; attempt<=RETRY_ATTEMPTS; attempt++ )); do
    if "$@" >/dev/null; then
      return 0
    fi
    if (( attempt < RETRY_ATTEMPTS )); then
      delay="${RETRY_DELAYS[attempt-1]}"
      log "WARN: $label failed (attempt $attempt/$RETRY_ATTEMPTS) — retrying in ${delay}s"
      sleep "$delay"
    fi
  done
  log "ERROR: $label failed after $RETRY_ATTEMPTS attempts"
  return 1
}

# Prefer the native CLI: launchd's login shell never reads ~/.zshrc, so bare
# PATH resolution picks up whatever /opt/homebrew/bin carries — observed
# 2026-08-05: a Feb-era npm 2.1.56 whose marketplace refresh re-clones
# SSH-only with no backup/restore. The native CLI probes transport
# (HTTPS-primary here) and restores the marketplace dir on clone failure.
PATH="$HOME/.local/bin:$PATH"

command -v claude >/dev/null || { log "ERROR: claude CLI not on PATH"; exit 1; }
command -v jq >/dev/null || { log "ERROR: jq not on PATH"; exit 1; }
log "claude binary: $(command -v claude) ($(claude --version 2>/dev/null || echo version-unknown))"

# Rollback state (fork copy uninstalled) makes the hop a deliberate no-op;
# rollback step 6 unloads the launchd job, but don't spam errors if it ran.
if ! claude plugin list 2>/dev/null | grep -q "$PLUGIN"; then
  log "fork plugin $PLUGIN not installed — no-op (rollback state?)"
  exit 0
fi

log "updating marketplace $MARKETPLACE..."
retry_cli "marketplace update" claude plugin marketplace update "$MARKETPLACE"

[[ -f "$MARKETPLACE_MANIFEST" ]] || { log "ERROR: marketplace manifest missing at $MARKETPLACE_MANIFEST"; exit 1; }
expected=$(jq -r '.plugins[0].version' "$MARKETPLACE_MANIFEST")
[[ -n "$expected" && "$expected" != "null" ]] || { log "ERROR: could not read fork release version from marketplace manifest"; exit 1; }

if [[ -f "$CACHE_ROOT/$expected/.claude-plugin/plugin.json" ]]; then
  cached=$(jq -r '.version' "$CACHE_ROOT/$expected/.claude-plugin/plugin.json")
  if [[ "$cached" == "$expected" ]]; then
    log "in sync at $expected — nothing to do"
    exit 0
  fi
fi

log "fork release $expected not yet delivered — updating plugin..."
retry_cli "plugin update" claude plugin update "$PLUGIN"

cache_manifest="$CACHE_ROOT/$expected/.claude-plugin/plugin.json"
[[ -f "$cache_manifest" ]] || { log "ERROR: update ran but cache dir for $expected is missing ($cache_manifest)"; exit 1; }
delivered=$(jq -r '.version' "$cache_manifest")
[[ "$delivered" == "$expected" ]] || { log "ERROR: cache plugin.json says $delivered, expected $expected"; exit 1; }

log "delivered $expected (cache plugin.json verified); running sessions keep their started version, new sessions load $expected"
