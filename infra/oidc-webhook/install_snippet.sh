#!/usr/bin/env bash
# ============================================================================
# SDLCMA — install the OIDC notifier jobs into a monitored project's
# .gitlab-ci.yml, via the GitLab API. DRY-RUN BY DEFAULT.
#
#   bash infra/oidc-webhook/install_snippet.sh              # show the diff
#   APPLY=1 bash infra/oidc-webhook/install_snippet.sh      # commit it
#
# Idempotent by a marker block:
#
#   # >>> sdlcma-oidc-notify (managed by infra/oidc-webhook/install_snippet.sh)
#   ... the two jobs ...
#   # <<< sdlcma-oidc-notify
#
# Re-running REPLACES the block rather than appending a second copy, so
# updating the snippet is the same command. Removing the block by hand (or
# with UNINSTALL=1) fully reverses it.
#
# WHY A MARKER BLOCK AND NOT `include:`
# -------------------------------------
# `include: local:` would be tidier, but it means merging a YAML key into a
# file we do not own — and `include` may be absent, a string, or a list, each
# needing different surgery. A fenced block is append-only text: it cannot
# corrupt the caller's existing pipeline definition, and a human reading the
# file can see exactly what is ours and delete it with one editor motion.
# Revisit if this ever has to scale past a handful of projects.
#
# ⚠️ Committing to the default branch FIRES A PIPELINE on that branch. That is
#    usually what you want (it exercises the notifier immediately), but be
#    aware: if that pipeline FAILS and an orchestrator is running, a bug-fix
#    worker will be spawned for real.
#
# Overrides: GITLAB_URL, GATEWAY_ENV, PROJECT_PATH, BRANCH, UNINSTALL=1
# ============================================================================
set -euo pipefail

REPO_DIR="${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
GITLAB_URL="${GITLAB_URL:-http://minus:8929}"
GATEWAY_ENV="${GATEWAY_ENV:-local_multi_process}"
PROJECT_PATH="${PROJECT_PATH:-root/sdlcma-fix-f01-off-by-one}"
BRANCH="${BRANCH:-}"
APPLY="${APPLY:-0}"
UNINSTALL="${UNINSTALL:-0}"
CURL_TIMEOUT="${CURL_TIMEOUT:-20}"
SNIPPET_FILE="${REPO_DIR}/infra/oidc-webhook/gitlab-ci-snippet.yml"
CI_FILE=".gitlab-ci.yml"

BEGIN_MARK="# >>> sdlcma-oidc-notify (managed by infra/oidc-webhook/install_snippet.sh)"
END_MARK="# <<< sdlcma-oidc-notify"

say()  { printf '\n=== %s ===\n' "$*"; }
info() { printf '    %s\n' "$*"; }
die()  { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

[ -f "${SNIPPET_FILE}" ] || die "missing ${SNIPPET_FILE}"

# ---- token (never echoed) --------------------------------------------------
WORKER_ENV_FILE="${REPO_DIR}/settings/worker_${GATEWAY_ENV}.env"
GITLAB_TOKEN="${GITLAB_PRIVATE_TOKEN:-}"
if [ -z "${GITLAB_TOKEN}" ] && [ -f "${WORKER_ENV_FILE}" ]; then
  GITLAB_TOKEN="$(grep -E '^GITLAB_PRIVATE_TOKEN=' "${WORKER_ENV_FILE}" \
    | tail -1 | cut -d= -f2- | tr -d '"'"'"' \r')"
fi
[ -n "${GITLAB_TOKEN}" ] || die "no GitLab token (env GITLAB_PRIVATE_TOKEN or ${WORKER_ENV_FILE})"

PROJ_ENC="$(printf '%s' "${PROJECT_PATH}" | sed 's#/#%2F#g')"
API="${GITLAB_URL%/}/api/v4/projects/${PROJ_ENC}"

say "project ${PROJECT_PATH}"

PROJ_JSON="$(curl -s -f --max-time "${CURL_TIMEOUT}" -H "PRIVATE-TOKEN: ${GITLAB_TOKEN}" "${API}")" \
  || die "cannot read project ${PROJECT_PATH}"

if [ -z "${BRANCH}" ]; then
  BRANCH="$(printf '%s' "${PROJ_JSON}" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("default_branch",""))')"
fi
[ -n "${BRANCH}" ] || die "could not determine the default branch"
info "branch = ${BRANCH}"

# ---- current .gitlab-ci.yml ------------------------------------------------
RAW_URL="${API}/repository/files/$(printf '%s' "${CI_FILE}" | sed 's/\./%2E/g')/raw?ref=${BRANCH}"
CURRENT="$(curl -s --max-time "${CURL_TIMEOUT}" -H "PRIVATE-TOKEN: ${GITLAB_TOKEN}" "${RAW_URL}" || true)"

FILE_EXISTS=1
case "${CURRENT}" in
  '{"message":"404'*|'') FILE_EXISTS=0; CURRENT="" ;;
esac
info "${CI_FILE} exists = $([ "${FILE_EXISTS}" = 1 ] && echo yes || echo no)"

# ---- build the new content -------------------------------------------------
# ⚠️ CURRENT goes in through the ENVIRONMENT, not stdin. `python3 - <<'PYEOF'`
# already uses stdin to feed the script, so a second `<<< "$CURRENT"` silently
# wins and the script reads an EMPTY stdin — which made this function return
# just the marker block and DROP the project's existing pipeline. Caught only
# because the dry-run is the default. Do not "simplify" this back to a pipe.
NEW_CONTENT="$(BEGIN_MARK="${BEGIN_MARK}" END_MARK="${END_MARK}" \
  UNINSTALL="${UNINSTALL}" SNIPPET_FILE="${SNIPPET_FILE}" \
  CURRENT="${CURRENT}" \
  python3 - <<'PYEOF'
import os, re, sys

begin = os.environ["BEGIN_MARK"]
end   = os.environ["END_MARK"]
current = os.environ["CURRENT"]

# Strip the marker block if present (this is both the uninstall path and the
# "replace, don't append a second copy" path).
pattern = re.compile(
    re.escape(begin) + r".*?" + re.escape(end) + r"[^\n]*\n?",
    re.DOTALL,
)
stripped = pattern.sub("", current).rstrip("\n")

if os.environ["UNINSTALL"] == "1":
    print(stripped + ("\n" if stripped else ""), end="")
    sys.exit(0)

# Take the snippet from its first YAML key onward — the file header is a long
# design rationale that belongs in the repo, not in every monitored project.
lines = open(os.environ["SNIPPET_FILE"]).read().splitlines()
start = next(i for i, l in enumerate(lines)
             if l and not l.startswith("#"))
body = "\n".join(lines[start:]).strip("\n")

out = stripped + ("\n\n" if stripped else "") + begin + "\n" + body + "\n" + end + "\n"
print(out, end="")
PYEOF
)"

if [ "${NEW_CONTENT}" = "${CURRENT}" ]; then
  say "no change needed — already in sync"
  exit 0
fi

# ---- show the diff ---------------------------------------------------------
say "diff"
diff -u <(printf '%s' "${CURRENT}") <(printf '%s' "${NEW_CONTENT}") \
  | sed 's/^/    /' || true

if [ "${APPLY}" != "1" ]; then
  say "DRY RUN — nothing committed"
  cat <<EOF

  To apply:

      APPLY=1 PROJECT_PATH=${PROJECT_PATH} bash \$0

  ⚠️ Committing to ${BRANCH} fires a pipeline on that branch. If it fails and
     an orchestrator is running, a real bug-fix worker will be spawned.

EOF
  exit 0
fi

# ---- commit ----------------------------------------------------------------
say "committing to ${BRANCH}"

ACTION="update"
[ "${FILE_EXISTS}" = 1 ] || ACTION="create"

MSG="$([ "${UNINSTALL}" = 1 ] \
  && echo 'chore: remove SDLCMA OIDC notifier jobs' \
  || echo 'chore: add SDLCMA OIDC notifier jobs (webhook auth)')"

PAYLOAD="$(CONTENT="${NEW_CONTENT}" BRANCH="${BRANCH}" ACTION="${ACTION}" \
  CI_FILE="${CI_FILE}" MSG="${MSG}" python3 -c '
import json, os
print(json.dumps({
    "branch": os.environ["BRANCH"],
    "commit_message": os.environ["MSG"],
    "actions": [{
        "action": os.environ["ACTION"],
        "file_path": os.environ["CI_FILE"],
        "content": os.environ["CONTENT"],
    }],
}))')"

CODE="$(curl -s -o /tmp/sdlcma_commit_resp.json -w '%{http_code}' --max-time "${CURL_TIMEOUT}" \
  -X POST "${API}/repository/commits" \
  -H "PRIVATE-TOKEN: ${GITLAB_TOKEN}" \
  -H 'Content-Type: application/json' \
  --data-binary "${PAYLOAD}")"

case "${CODE}" in
  2*) info "committed (http ${CODE})"
      python3 -c '
import json
d = json.load(open("/tmp/sdlcma_commit_resp.json"))
print("    commit %s  %s" % (d.get("short_id"), d.get("title")))
print("    " + (d.get("web_url") or ""))
' ;;
  *)  printf '    response: '; cat /tmp/sdlcma_commit_resp.json; echo
      die "commit failed: http ${CODE}" ;;
esac

say "done"
cat <<EOF

  A pipeline is now running on ${BRANCH}. Watch the gateway for:

      phase_marker phase=webhook_auth result=accept project_id=...

  and the rejection counter for anything unexpected:

      curl -s http://localhost:8000/metrics | grep webhook_auth_rejected

EOF
