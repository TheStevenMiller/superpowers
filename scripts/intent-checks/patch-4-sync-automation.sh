#!/usr/bin/env bash
# Intent checks for [local] infra commit — upstream sync automation +
# update hop (RESEARCH.md §4 sync automation, findings #4/#5/#12).
#
# Run from the repo root; exit 0 means the operative surface is intact.
# Anchored form: operative-line anchors + negative assertions that the
# forbidden mechanisms are absent. If any check fails after an upstream
# sync, do not promote — open the sync-blocked issue.
set -euo pipefail

wf=.github/workflows/upstream-sync.yml

# Operative files present and executable
test -f "$wf"
test -x scripts/sync-candidate.sh
test -x scripts/promote-sync.sh
test -x scripts/update-hop.sh

# Workflow: single concurrency group (weekly + manual never overlap),
# unprivileged build job (upstream test code sees no token), wired to the
# candidate builder, lease-protected candidate pushes
grep -q 'group: upstream-sync' "$wf"
grep -q 'persist-credentials: false' "$wf"
grep -q 'sync-candidate.sh --output-dir' "$wf"
grep -q -- '--force-with-lease' "$wf"

# Workflow NEVER pushes main — candidate branches only (promotion is the
# human running promote-sync.sh)
! grep -E '^[^#]*git push.*refs/heads/main' "$wf"

# Candidate builder: collision-proof upstream tag namespace, desired-state
# no-op, fail-closed stack classification, finding-#4 tripwire
grep -q 'refs/upstream-tags/' scripts/sync-candidate.sh
grep -q 'result in-sync' scripts/sync-candidate.sh
grep -q 'tripwire' scripts/sync-candidate.sh

# Promotion: lease-protected main move + release cut via upstream's own
# bump mechanism, publication outside the patch stack
grep -q -- '--force-with-lease' scripts/promote-sync.sh
grep -q 'bump-version.sh' scripts/promote-sync.sh
grep -q 'chore(release)' scripts/promote-sync.sh

# Update hop: marketplace + plugin update, verified against the CACHE
# plugin.json only; the stale install-registry SHA field is never consulted
grep -q 'marketplace update' scripts/update-hop.sh
grep -q 'plugin update' scripts/update-hop.sh
grep -q 'plugin.json' scripts/update-hop.sh
! grep -E '^[^#]*gitCommitSha' scripts/update-hop.sh
! grep -E '^[^#]*installed_plugins' scripts/update-hop.sh

echo "patch-4 intent checks: PASS"
