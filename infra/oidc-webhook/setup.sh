#!/usr/bin/env bash
# ============================================================================
# SDLCMA — turn ON OIDC authentication + project authorization for the
# gateway's POST /webhook. Idempotent: safe to re-run. Counterpart: teardown.sh
#
# This is NOT a deployment harness (it starts no services). It configures an
# already-working GitLab-mode stack to require a GitLab-signed CI id_token,
# and it configures the monitored project to send one. Two sides:
#
#   gateway side  — writes WEBHOOK_AUTH_MODE / OIDC_ISSUER / OIDC_AUDIENCE
#                   into gateway/gateway_<ENV>.env (gitignored, per-host).
#   project side  — sets the three CI/CD variables the snippet needs, via the
#                   GitLab API, when a token is available.
#
# WHY THE ISSUER IS DISCOVERED, NOT TYPED
# ---------------------------------------
# OIDC_ISSUER must equal the token's `iss` BYTE FOR BYTE, and `iss` is whatever
# the GitLab instance calls itself (its external_url) — which is frequently NOT
# the URL you reach it on. Reaching a box at http://100.115.36.114:8929 while
# its external_url is http://minus:8929 is completely normal, and hand-copying
# the wrong one produces a 401 whose message ("token issuer does not match")
# reads like the token is bad rather than the config. So the script reads
# /.well-known/openid-configuration and uses what GitLab reports.
#
# ⚠️ Corollary: the JWKS URL may legitimately differ from the issuer. If the
# issuer hostname is not resolvable from the gateway, OIDC_JWKS_URL is pinned
# to a reachable address while OIDC_ISSUER keeps the issuer's own spelling.
# Those two settings exist precisely to be allowed to disagree.
#
# Usage (from anywhere):
#   OIDC_AUDIENCE=sdlcma-gateway.minus bash infra/oidc-webhook/setup.sh
#
# Common overrides:
#   GITLAB_URL=http://minus:8929        # how THIS host reaches GitLab
#   GATEWAY_ENV=local_multi_process     # which gateway_<ENV>.env to write
#   PROJECT_PATH=root/order_be          # monitored project (namespace/name)
#   GATEWAY_URL=http://<host>:8000      # where the CI job POSTs webhooks
#   SKIP_PROJECT_VARS=1                 # gateway side only, no GitLab writes
#
# Preconditions (one-time, not created here):
#   - A working GitLab-mode stack (see infra/local-gitlab/ or infra/k3s/).
#   - GitLab reachable at GITLAB_URL from this host.
#   - settings/worker_<ENV>.env holds GITLAB_PRIVATE_TOKEN (used for the
#     project-side writes; never printed).
# ============================================================================
set -euo pipefail

REPO_DIR="${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"

GITLAB_URL="${GITLAB_URL:-http://minus:8929}"
GATEWAY_ENV="${GATEWAY_ENV:-}"
PROJECT_PATH="${PROJECT_PATH:-root/order_be}"
GATEWAY_URL="${GATEWAY_URL:-}"
OIDC_AUDIENCE="${OIDC_AUDIENCE:-}"
SKIP_PROJECT_VARS="${SKIP_PROJECT_VARS:-0}"
CURL_TIMEOUT="${CURL_TIMEOUT:-20}"

say()  { printf '\n=== %s ===\n' "$*"; }
info() { printf '    %s\n' "$*"; }
die()  { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# 0. resolve which env file we are configuring
# ---------------------------------------------------------------------------
say "resolving gateway env"

if [ -z "${GATEWAY_ENV}" ]; then
  # Same two-step probe the app itself does: gateway/.env names the ENV.
  if [ -f "${REPO_DIR}/gateway/.env" ]; then
    GATEWAY_ENV="$(grep -E '^ENV=' "${REPO_DIR}/gateway/.env" | tail -1 | cut -d= -f2- | tr -d '"'"'"' \r')"
  fi
  GATEWAY_ENV="${GATEWAY_ENV:-local_multi_process}"
fi

GW_ENV_FILE="${REPO_DIR}/gateway/gateway_${GATEWAY_ENV}.env"
WORKER_ENV_FILE="${REPO_DIR}/settings/worker_${GATEWAY_ENV}.env"

info "ENV          = ${GATEWAY_ENV}"
info "gateway env  = ${GW_ENV_FILE}"
[ -f "${GW_ENV_FILE}" ] || die "no such file: ${GW_ENV_FILE} (copy the .example first)"

# OIDC_AUDIENCE has no default ANYWHERE — not in the settings class, not here.
# GitLab mints a token for whatever audience a job asks for, so a guessable
# audience means any project on the instance can forge a trigger. A default
# would be guessable by construction.
if [ -z "${OIDC_AUDIENCE}" ]; then
  die "OIDC_AUDIENCE is required and has no default (a guessable audience lets
       any project on this GitLab forge a trigger). Pick something specific to
       THIS deployment, e.g.

         OIDC_AUDIENCE=sdlcma-gateway.${GATEWAY_ENV} bash \$0"
fi

# ---------------------------------------------------------------------------
# 1. discover the issuer from GitLab itself
# ---------------------------------------------------------------------------
say "discovering OIDC issuer from ${GITLAB_URL}"

DISCOVERY="$(curl -sS --max-time "${CURL_TIMEOUT}" \
  "${GITLAB_URL%/}/.well-known/openid-configuration" 2>/dev/null || true)"

[ -n "${DISCOVERY}" ] || die "no response from ${GITLAB_URL}/.well-known/openid-configuration
       GitLab is unreachable or not running.

         Omnibus (native install):   sudo gitlab-ctl status
                                     sudo gitlab-ctl start
         Container:                  docker ps -a --filter name=gitlab

       ⚠️ An Omnibus GitLab installed inside WSL does NOT come back on its own
          after a WSL restart — runit is started by systemd, which WSL does not
          run by default. So 'GitLab is down' after booting the WSL distro is
          the expected state, not evidence that someone stopped it.

       Give it 1-3 min after start: the API answers well before the UI does,
       but not immediately."

read -r OIDC_ISSUER DISCOVERED_JWKS ALGS <<EOF
$(printf '%s' "${DISCOVERY}" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print("PARSE_ERROR - -"); sys.exit(0)
print(d.get("issuer", "-"),
      d.get("jwks_uri", "-"),
      ",".join(d.get("id_token_signing_alg_values_supported", []) or ["-"]))
')
EOF

[ "${OIDC_ISSUER}" != "PARSE_ERROR" ] || die "discovery endpoint did not return JSON.
       A login/redirect page usually means the URL is wrong or GitLab is still
       booting (an Omnibus container needs 2-4 min before the API answers)."

info "issuer       = ${OIDC_ISSUER}"
info "jwks_uri     = ${DISCOVERED_JWKS}"
info "algs         = ${ALGS}"

case ",${ALGS}," in
  *,RS256,*) : ;;
  *) die "this GitLab does not advertise RS256 (${ALGS}). gateway/webhook_auth.py
       pins algorithms=[\"RS256\"] on purpose — widening it re-enables alg:none
       and the RS256->HS256 downgrade. Do not 'fix' this by relaxing the pin." ;;
esac

# ---------------------------------------------------------------------------
# 2. does the ISSUER's own hostname resolve here? if not, pin the JWKS URL.
# ---------------------------------------------------------------------------
say "checking JWKS reachability"

ISSUER_HOST="$(printf '%s' "${OIDC_ISSUER}" | sed -E 's#^[a-z]+://##; s#[:/].*$##')"
OIDC_JWKS_URL=""

if curl -sS -o /dev/null --max-time "${CURL_TIMEOUT}" "${DISCOVERED_JWKS}" 2>/dev/null; then
  info "JWKS reachable at the advertised URL — leaving OIDC_JWKS_URL empty"
  info "(the gateway derives {issuer}/oauth/discovery/keys by itself)"
else
  # The issuer spelling is unreachable from here. Keep OIDC_ISSUER as GitLab
  # spells it (it must match `iss` verbatim) and point the key fetch at the
  # address we actually reached.
  OIDC_JWKS_URL="${GITLAB_URL%/}/oauth/discovery/keys"
  info "advertised JWKS unreachable (issuer host '${ISSUER_HOST}' not resolvable here)"
  info "pinning OIDC_JWKS_URL=${OIDC_JWKS_URL}"
  curl -sS -o /dev/null --max-time "${CURL_TIMEOUT}" "${OIDC_JWKS_URL}" \
    || die "neither ${DISCOVERED_JWKS} nor ${OIDC_JWKS_URL} is reachable"
fi

# ---------------------------------------------------------------------------
# 3. write the gateway settings (idempotent upsert)
# ---------------------------------------------------------------------------
say "writing ${GW_ENV_FILE}"

python3 - "${GW_ENV_FILE}" <<PYEOF
import os, sys, re
path = sys.argv[1]
pairs = [
    ("WEBHOOK_AUTH_MODE", "oidc"),
    ("OIDC_ISSUER",       """${OIDC_ISSUER}"""),
    ("OIDC_AUDIENCE",     """${OIDC_AUDIENCE}"""),
    ("OIDC_JWKS_URL",     """${OIDC_JWKS_URL}"""),
]
with open(path) as fh:
    lines = fh.read().splitlines()

for key, val in pairs:
    rx = re.compile(rf"^\s*#?\s*{re.escape(key)}\s*=")
    for i, line in enumerate(lines):
        if rx.match(line):
            lines[i] = f"{key}={val}"
            break
    else:
        lines.append(f"{key}={val}")

with open(path, "w") as fh:
    fh.write("\n".join(lines).rstrip("\n") + "\n")
print("    " + "\n    ".join(f"{k}={v}" for k, v in pairs))
PYEOF

# ---------------------------------------------------------------------------
# 4. project side — CI/CD variables
# ---------------------------------------------------------------------------
if [ "${SKIP_PROJECT_VARS}" = "1" ]; then
  say "skipping project-side variables (SKIP_PROJECT_VARS=1)"
else
  say "configuring CI/CD variables on ${PROJECT_PATH}"

  # Read the token WITHOUT echoing it. Never `set -x` past this point.
  GITLAB_TOKEN="${GITLAB_PRIVATE_TOKEN:-}"
  if [ -z "${GITLAB_TOKEN}" ] && [ -f "${WORKER_ENV_FILE}" ]; then
    GITLAB_TOKEN="$(grep -E '^GITLAB_PRIVATE_TOKEN=' "${WORKER_ENV_FILE}" \
      | tail -1 | cut -d= -f2- | tr -d '"'"'"' \r')"
  fi
  [ -n "${GITLAB_TOKEN}" ] || die "no GitLab token found (env GITLAB_PRIVATE_TOKEN
       or ${WORKER_ENV_FILE}). Re-run with SKIP_PROJECT_VARS=1 to configure the
       gateway only and set the project variables by hand."

  if [ -z "${GATEWAY_URL}" ]; then
    # The CI job runs on the GitLab side and must reach this host by an address
    # that is routable from there — localhost is never right.
    _ts_ip="$(command -v tailscale >/dev/null 2>&1 && tailscale ip -4 2>/dev/null | head -1 || true)"
    [ -n "${_ts_ip}" ] || die "GATEWAY_URL not set and no tailscale IP found.
       Set it to an address the GitLab runner can reach, e.g.
         GATEWAY_URL=http://<this-host>:8000"
    GATEWAY_URL="http://${_ts_ip}:8000"
    info "GATEWAY_URL not set — defaulting to this host's tailnet address"
  fi
  info "GATEWAY_URL  = ${GATEWAY_URL}"

  PROJ_ENC="$(printf '%s' "${PROJECT_PATH}" | sed 's#/#%2F#g')"
  API="${GITLAB_URL%/}/api/v4/projects/${PROJ_ENC}"

  # The namespace differs per GitLab instance (gitlab.com vs a self-hosted box
  # vs a local compose stack all host "order_be" under different owners), so a
  # 404 here is a routine first-run event, not an error worth a scavenger hunt.
  # List what this token CAN see instead of making the operator guess.
  if ! curl -s -o /dev/null -f --max-time "${CURL_TIMEOUT}" \
         -H "PRIVATE-TOKEN: ${GITLAB_TOKEN}" "${API}"; then
    printf '\n    project %s is not readable. Projects visible to this token:\n\n' "${PROJECT_PATH}"
    curl -s --max-time "${CURL_TIMEOUT}" -H "PRIVATE-TOKEN: ${GITLAB_TOKEN}" \
      "${GITLAB_URL%/}/api/v4/projects?simple=true&per_page=100&order_by=last_activity_at" \
      | python3 -c '
import json, sys
try:
    rows = json.load(sys.stdin)
except Exception:
    rows = []
if not rows:
    print("      (none — the token may lack api scope)")
for p in rows:
    print("      %-45s id=%s" % (p.get("path_with_namespace"), p.get("id")))
' || true
    printf '\n'
    die "re-run with PROJECT_PATH=<one of the above>"
  fi

  # Idempotent upsert: PUT updates, and on 404 (variable absent) POST creates.
  set_var() {
    local key="$1" val="$2" masked="$3"
    local code
    code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time "${CURL_TIMEOUT}" \
      -X PUT "${API}/variables/${key}" \
      -H "PRIVATE-TOKEN: ${GITLAB_TOKEN}" \
      --data-urlencode "value=${val}" \
      --data "masked=${masked}" --data "protected=false")"
    if [ "${code}" = "404" ]; then
      code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time "${CURL_TIMEOUT}" \
        -X POST "${API}/variables" \
        -H "PRIVATE-TOKEN: ${GITLAB_TOKEN}" \
        --data-urlencode "key=${key}" \
        --data-urlencode "value=${val}" \
        --data "masked=${masked}" --data "protected=false")"
    fi
    case "${code}" in
      2*) info "${key} set (http ${code})" ;;
      *)  die "failed to set ${key}: http ${code}" ;;
    esac
  }

  set_var SDLCMA_GATEWAY_URL "${GATEWAY_URL}" false
  set_var SDLCMA_AUDIENCE    "${OIDC_AUDIENCE}" false

  # SDLCMA_CI_READ_TOKEN is deliberately NOT set here. It is a separate,
  # narrower credential (read_api on this project only) whose whole point is
  # that it is not the stack's main token. Reusing GITLAB_PRIVATE_TOKEN would
  # hand a full-scope token to every job in the project.
  info "SDLCMA_CI_READ_TOKEN NOT set — create a read_api project token and add"
  info "it by hand (masked). Without it the notifier still fires but reports"
  info "its own job id, so the worker fetches the wrong trace."
fi

# ---------------------------------------------------------------------------
# 5. next steps
# ---------------------------------------------------------------------------
say "done — gateway is configured, but NOT yet restarted"

cat <<EOF

  1. Add the notifier jobs to ${PROJECT_PATH}'s .gitlab-ci.yml:

         include:
           - remote: '<url to infra/oidc-webhook/gitlab-ci-snippet.yml>'
       or paste the two jobs from that file directly.

     ⚠️ BOTH jobs are required. sdlcma_notify_success is not optional: the
        worker's wait_ci_result blocks until it hears the fix branch went
        green, so shipping only the failure half makes every successful fix
        time out and route to handle_failure instead of opening an MR.

  2. Create SDLCMA_CI_READ_TOKEN (read_api, this project) and add it as a
     masked CI/CD variable.

  3. Restart the gateway so it picks up ${GW_ENV_FILE}.

  4. Verify — an unauthenticated POST must now be refused:

         curl -s -o /dev/null -w '%{http_code}\\n' \\
           -X POST ${GATEWAY_URL:-http://<gateway>:8000}/webhook \\
           -H 'Content-Type: application/json' -d '{}'
         # expect 401

     Then trigger a failing pipeline and watch:

         phase_marker phase=webhook_auth result=accept project_id=...

  Rollback at any point:  bash infra/oidc-webhook/teardown.sh

EOF
