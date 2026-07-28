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
# sessions pick up the new version.
set -euo pipefail

MARKETPLACE="superpowers-dev"
PLUGIN="superpowers@superpowers-dev"
MARKETPLACE_MANIFEST="$HOME/.claude/plugins/marketplaces/$MARKETPLACE/.claude-plugin/marketplace.json"
CACHE_ROOT="$HOME/.claude/plugins/cache/$MARKETPLACE/superpowers"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

command -v claude >/dev/null || { log "ERROR: claude CLI not on PATH"; exit 1; }
command -v jq >/dev/null || { log "ERROR: jq not on PATH"; exit 1; }

# Rollback state (fork copy uninstalled) makes the hop a deliberate no-op;
# rollback step 6 unloads the launchd job, but don't spam errors if it ran.
if ! claude plugin list 2>/dev/null | grep -q "$PLUGIN"; then
  log "fork plugin $PLUGIN not installed — no-op (rollback state?)"
  exit 0
fi

log "updating marketplace $MARKETPLACE..."
claude plugin marketplace update "$MARKETPLACE" >/dev/null

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
claude plugin update "$PLUGIN" >/dev/null

cache_manifest="$CACHE_ROOT/$expected/.claude-plugin/plugin.json"
[[ -f "$cache_manifest" ]] || { log "ERROR: update ran but cache dir for $expected is missing ($cache_manifest)"; exit 1; }
delivered=$(jq -r '.version' "$cache_manifest")
[[ "$delivered" == "$expected" ]] || { log "ERROR: cache plugin.json says $delivered, expected $expected"; exit 1; }

log "delivered $expected (cache plugin.json verified); running sessions keep their started version, new sessions load $expected"
