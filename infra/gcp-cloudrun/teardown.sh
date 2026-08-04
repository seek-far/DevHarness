#!/usr/bin/env bash
# ============================================================================
# SDLCMA — remove the Cloud Run LLM Gateway deployment. Counterpart: setup.sh
#
# Deletes in dependency order: service → images → repository → service account.
# Every step tolerates "already gone", so this is safe to re-run and safe to
# run against a partial setup.
#
# The IAM binding is removed too. Leaving a service account with
# roles/aiplatform.user behind after deleting the thing that used it is how
# projects accumulate identities nobody can account for later.
#
#   bash infra/gcp-cloudrun/teardown.sh
#   KEEP_REPO=1 bash infra/gcp-cloudrun/teardown.sh   # keep Artifact Registry
# ============================================================================
set -euo pipefail

PROJECT="${PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${REGION:-us-central1}"
AR_REPO="${AR_REPO:-sdlcma}"
SERVICE="${SERVICE:-llm-gateway}"
SA_NAME="${SA_NAME:-llm-gateway-sa}"
KEEP_REPO="${KEEP_REPO:-0}"
REPO_DIR="${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"

SA_EMAIL="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"

say() { printf '\n=== %s ===\n' "$*"; }

[ -n "$PROJECT" ] || { echo "no project set" >&2; exit 1; }
say "removing from project=${PROJECT} region=${REGION}"

say "1. Cloud Run service"
if gcloud run services describe "$SERVICE" --region "$REGION" \
     --project "$PROJECT" >/dev/null 2>&1; then
  gcloud run services delete "$SERVICE" --region "$REGION" \
    --project "$PROJECT" --quiet
  echo "  deleted ${SERVICE}"
else
  echo "  ${SERVICE} not present"
fi

say "2. Artifact Registry"
if [ "$KEEP_REPO" = "1" ]; then
  echo "  KEEP_REPO=1 — leaving ${AR_REPO} in place"
elif gcloud artifacts repositories describe "$AR_REPO" \
       --location "$REGION" --project "$PROJECT" >/dev/null 2>&1; then
  gcloud artifacts repositories delete "$AR_REPO" \
    --location "$REGION" --project "$PROJECT" --quiet
  echo "  deleted repository ${AR_REPO} (and the images in it)"
else
  echo "  ${AR_REPO} not present"
fi

say "3. service account + IAM binding"
if gcloud iam service-accounts describe "$SA_EMAIL" --project "$PROJECT" >/dev/null 2>&1; then
  gcloud projects remove-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="roles/aiplatform.user" --condition=None --quiet >/dev/null 2>&1 || true
  gcloud iam service-accounts delete "$SA_EMAIL" --project "$PROJECT" --quiet
  echo "  deleted ${SA_NAME} and its aiplatform.user binding"
else
  echo "  ${SA_NAME} not present"
fi

say "4. rendered local config"
rm -f "${REPO_DIR}/configs/llm_gateway/vertex_cloudrun.local.yaml"
echo "  removed vertex_cloudrun.local.yaml (if any)"

cat <<EOF

=== done ===
  Nothing left billable. The two APIs stay enabled — enabling costs nothing,
  and disabling them would affect anything else in the project that uses them.
EOF
