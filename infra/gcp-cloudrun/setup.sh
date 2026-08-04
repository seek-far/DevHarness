#!/usr/bin/env bash
# ============================================================================
# SDLCMA — deploy the LLM Gateway to Google Cloud Run. Counterpart: teardown.sh
#
# The gateway is the ONLY one of the four services that fits Cloud Run:
# stateless HTTP, no Redis, already containerised. The orchestrator holds an
# in-memory WorkerRegistry and consumes a Redis stream (it would need
# min-instances=1 + always-on CPU + a VPC connector — i.e. a worse GCE); the
# webhook gateway needs Redis; workers need Redis and, for ver99, the host
# docker.sock. Do not "extend" this harness to them.
#
# What this actually proves, beyond "a service is up": on Cloud Run the
# container runs AS A SERVICE ACCOUNT, so google.auth.default() inside
# llm_gateway/gcp_auth.py resolves to that SA rather than to your local ADC
# user. That is the half of the keyless-auth path a laptop cannot exercise.
#
# ACCESS MODEL — authenticated, no public URL (deliberate). The service is
# deployed WITHOUT --allow-unauthenticated, because this gateway has no auth
# of its own: a leaked *.run.app URL would be an open door onto your Vertex
# quota. Clients reach it through `gcloud run services proxy`, which handles
# IAM locally, so the worker keeps pointing at http://localhost:9000/v1 and
# needs no code change. See README.md for the two alternatives.
#
# Idempotent: re-running creates nothing twice and redeploys a fresh revision.
#
#   bash infra/gcp-cloudrun/setup.sh
#   REGION=europe-west4 IMAGE_TAG=v2 bash infra/gcp-cloudrun/setup.sh
# ============================================================================
set -euo pipefail

REPO_DIR="${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
PROJECT="${PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${REGION:-us-central1}"
AR_REPO="${AR_REPO:-sdlcma}"
SERVICE="${SERVICE:-llm-gateway}"
SA_NAME="${SA_NAME:-llm-gateway-sa}"
IMAGE_TAG="${IMAGE_TAG:-v1}"
# The container listens on 9000 (Dockerfile.llm-gateway). Cloud Run defaults to
# sending traffic to 8080, so --port below is NOT optional: without it the
# revision fails its health check and the error reads like a crash loop.
CONTAINER_PORT="${CONTAINER_PORT:-9000}"

SA_EMAIL="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${AR_REPO}/${SERVICE}:${IMAGE_TAG}"
CFG_TEMPLATE="${REPO_DIR}/configs/llm_gateway/vertex_cloudrun.yaml"
CFG_LOCAL="${REPO_DIR}/configs/llm_gateway/vertex_cloudrun.local.yaml"
CFG_IN_IMAGE="/app/configs/llm_gateway/vertex_cloudrun.local.yaml"

say() { printf '\n=== %s ===\n' "$*"; }

[ -n "$PROJECT" ] || { echo "no project set: gcloud config set project <ID>" >&2; exit 1; }
say "target"
echo "  project=${PROJECT} region=${REGION}"
echo "  image=${IMAGE}"
echo "  service account=${SA_EMAIL}"

say "1. APIs"
# Enabling is free and idempotent. Note APIs take a minute or two to propagate;
# an immediate push/deploy right after a first-time enable can still 403.
gcloud services enable run.googleapis.com artifactregistry.googleapis.com \
  --project "$PROJECT" --quiet
echo "  run + artifactregistry enabled"

say "2. Artifact Registry repository"
if gcloud artifacts repositories describe "$AR_REPO" \
     --location "$REGION" --project "$PROJECT" >/dev/null 2>&1; then
  echo "  ${AR_REPO} already exists"
else
  gcloud artifacts repositories create "$AR_REPO" \
    --repository-format=docker --location "$REGION" --project "$PROJECT" \
    --description="SDLCMA container images"
  echo "  created ${AR_REPO}"
fi
gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet

say "3. runtime service account"
if gcloud iam service-accounts describe "$SA_EMAIL" --project "$PROJECT" >/dev/null 2>&1; then
  echo "  ${SA_NAME} already exists"
else
  gcloud iam service-accounts create "$SA_NAME" --project "$PROJECT" \
    --display-name="SDLCMA LLM Gateway (Cloud Run runtime)"
  echo "  created ${SA_NAME}"
fi
# Least privilege ON PURPOSE: the default Compute Engine SA carries Editor,
# which would let a compromised gateway rewrite the whole project. This one
# gets exactly the Vertex data-plane role and nothing else.
#
# ⚠️ IAM IS EVENTUALLY CONSISTENT. A service account created seconds ago is
# routinely still invisible to add-iam-policy-binding, which then fails with
#     INVALID_ARGUMENT: Service account <sa> does not exist
# immediately after gcloud printed "Created service account". Hit on the very
# first run of this harness (2026-08-04). It is a propagation window, not an
# error — so retry instead of aborting. add-iam-policy-binding is itself
# idempotent, which is what makes retrying safe.
for attempt in $(seq 1 12); do
  if gcloud projects add-iam-policy-binding "$PROJECT" \
       --member="serviceAccount:${SA_EMAIL}" \
       --role="roles/aiplatform.user" --condition=None --quiet >/dev/null 2>&1; then
    echo "  granted roles/aiplatform.user (attempt ${attempt})"
    break
  fi
  [ "$attempt" -eq 12 ] && {
    echo "  could not bind roles/aiplatform.user after 12 attempts" >&2
    echo "  (if this is not propagation, check: gcloud iam service-accounts describe ${SA_EMAIL})" >&2
    exit 1; }
  sleep 5
done

say "4. render config (project id into the template)"
[ -f "$CFG_TEMPLATE" ] || { echo "missing ${CFG_TEMPLATE}" >&2; exit 1; }
sed "s/__PROJECT_ID__/${PROJECT}/g" "$CFG_TEMPLATE" > "$CFG_LOCAL"
grep -q "__PROJECT_ID__" "$CFG_LOCAL" && { echo "substitution failed" >&2; exit 1; }
echo "  wrote $(basename "$CFG_LOCAL") (gitignored)"

say "5. build + push image"
docker build -f "${REPO_DIR}/Dockerfile.llm-gateway" -t "$IMAGE" "$REPO_DIR"
docker push "$IMAGE"

say "6. deploy revision"
# --no-allow-unauthenticated is the access model (see header). --port is
# mandatory (container listens on 9000, Cloud Run would otherwise probe 8080).
gcloud run deploy "$SERVICE" \
  --image "$IMAGE" \
  --region "$REGION" \
  --project "$PROJECT" \
  --service-account "$SA_EMAIL" \
  --port "$CONTAINER_PORT" \
  --no-allow-unauthenticated \
  --set-env-vars "LLM_GATEWAY_CONFIG=${CFG_IN_IMAGE}" \
  --memory 512Mi \
  --quiet

URL="$(gcloud run services describe "$SERVICE" --region "$REGION" \
        --project "$PROJECT" --format='value(status.url)')"

say "7. verify (authenticated — the URL is not public)"
code="$(curl -s -o /dev/null -w '%{http_code}' \
  -H "Authorization: Bearer $(gcloud auth print-identity-token)" \
  "${URL}/health" || true)"
echo "  GET /health → HTTP ${code}"
[ "$code" = "200" ] || { echo "health check failed; see: gcloud run services logs read ${SERVICE} --region ${REGION}" >&2; exit 1; }

# Anonymous must FAIL — that is the access model working, not an error.
anon="$(curl -s -o /dev/null -w '%{http_code}' "${URL}/health" || true)"
echo "  GET /health without a token → HTTP ${anon} (expected 401/403)"
case "$anon" in 401|403) ;; *) echo "  WARNING: service appears publicly reachable" >&2 ;; esac

say "8. client-side preflight"
# ⚠️ BOTH of these were hit for real on the first run of this harness, and the
# second one produced a FALSE PASS — the smoke test "succeeded" against a
# locally-running gateway while the proxy had never started. A local port is
# indistinguishable from a tunnelled one once something answers on it.
if ss -ltn 2>/dev/null | grep -q ':9000 '; then
  echo "  ⚠️  something is ALREADY listening on :9000 — stop it before running"
  echo "     the proxy, or a smoke test will silently talk to that instead and"
  echo "     look like it passed. Tell them apart: a Cloud Run run has a"
  echo "     non-zero X-Sdlcma-Cost-Usd (this deployment disables the cache)."
else
  echo "  :9000 is free"
fi
echo "  NOTE: 'gcloud run services proxy' needs the cloud-run-proxy component."
echo "        On an apt-installed gcloud the component manager is disabled, so:"
echo "          sudo apt-get install google-cloud-cli-cloud-run-proxy"
echo "        Without it the proxy exits immediately (asking to install it)."

cat <<EOF

=== done ===
  service URL : ${URL}   (authenticated only)

  Verify inference without any proxy at all:

    curl -sS -X POST ${URL}/v1/chat/completions \\
      -H "Authorization: Bearer \$(gcloud auth print-identity-token)" \\
      -H 'Content-Type: application/json' \\
      -d '{"model":"x","messages":[{"role":"user","content":"say hi"}]}' -D-

    Expect HTTP 200 plus X-Sdlcma-Backend-Name and a NON-ZERO
    X-Sdlcma-Cost-Usd — the cache is disabled here, so a zero cost means you
    are talking to some other gateway.

  Point a worker at it WITHOUT changing any worker code:

    gcloud run services proxy ${SERVICE} --region ${REGION} --port=9000

  then, in another shell:

    LLM_VIA_GATEWAY=true \\
    LLM_API_BASE_URL=http://localhost:9000/v1 \\
    LLM_MODEL=google/gemini-2.5-flash \\
      uv run python -m bf_worker.standalone --source-dir <dir> --test-cmd pytest \\
        --no-git --output-dir /tmp/out --bug-id BUG-CR-1

  Logs:  gcloud run services logs read ${SERVICE} --region ${REGION}
  Undo:  bash infra/gcp-cloudrun/teardown.sh
EOF
