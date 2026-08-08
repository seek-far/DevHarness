#!/usr/bin/env bash
# ============================================================================
# SDLCMA — turn OIDC webhook authentication back OFF. Counterpart to setup.sh.
#
# Sets WEBHOOK_AUTH_MODE=none in gateway/gateway_<ENV>.env and leaves the
# OIDC_* values in place, commented-out-by-irrelevance: with the mode off they
# are never read, and keeping them means re-enabling is one word rather than a
# rediscovery. `none` restores the pre-auth code path exactly.
#
# Deliberately does NOT touch the project side. Removing the CI variables or
# the notifier jobs would stop webhooks reaching the gateway AT ALL, which is
# a much bigger outage than turning auth off — and the notifier jobs are a
# perfectly good trigger mechanism without OIDC. If you want to go all the way
# back to GitLab's own webhook delivery, re-enable that webhook first, THEN
# remove the snippet.
#
# Usage:
#   bash infra/oidc-webhook/teardown.sh
#   GATEWAY_ENV=local_multi_process bash infra/oidc-webhook/teardown.sh
# ============================================================================
set -euo pipefail

REPO_DIR="${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
GATEWAY_ENV="${GATEWAY_ENV:-}"

say()  { printf '\n=== %s ===\n' "$*"; }
info() { printf '    %s\n' "$*"; }
die()  { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

if [ -z "${GATEWAY_ENV}" ]; then
  if [ -f "${REPO_DIR}/gateway/.env" ]; then
    GATEWAY_ENV="$(grep -E '^ENV=' "${REPO_DIR}/gateway/.env" | tail -1 | cut -d= -f2- | tr -d '"'"'"' \r')"
  fi
  GATEWAY_ENV="${GATEWAY_ENV:-local_multi_process}"
fi

GW_ENV_FILE="${REPO_DIR}/gateway/gateway_${GATEWAY_ENV}.env"
[ -f "${GW_ENV_FILE}" ] || die "no such file: ${GW_ENV_FILE}"

say "disabling OIDC in ${GW_ENV_FILE}"

python3 - "${GW_ENV_FILE}" <<'PYEOF'
import re, sys
path = sys.argv[1]
with open(path) as fh:
    lines = fh.read().splitlines()

rx = re.compile(r"^\s*#?\s*WEBHOOK_AUTH_MODE\s*=")
for i, line in enumerate(lines):
    if rx.match(line):
        lines[i] = "WEBHOOK_AUTH_MODE=none"
        break
else:
    lines.append("WEBHOOK_AUTH_MODE=none")

with open(path, "w") as fh:
    fh.write("\n".join(lines).rstrip("\n") + "\n")
print("    WEBHOOK_AUTH_MODE=none")
PYEOF

info "OIDC_* values left in place (unread while mode=none)"

say "done — restart the gateway to apply"
cat <<'EOF'

  The /webhook endpoint is unauthenticated again. The notifier jobs in the
  monitored project keep working: they will send an Authorization header the
  gateway now ignores.

EOF
