# Cloud Run — LLM Gateway deployment

Eighth deployment harness, and the odd one out: it deploys **one service, not
the stack**. `setup.sh` puts `llm_gateway` on Google Cloud Run; `teardown.sh`
removes every resource it created.

```bash
bash infra/gcp-cloudrun/setup.sh        # deploy (idempotent)
bash infra/gcp-cloudrun/teardown.sh     # remove everything
```

## Why only the gateway

| Component | On Cloud Run? |
|---|---|
| **llm_gateway** | ✅ stateless HTTP, no Redis, already containerised |
| orchestrator | ❌ in-memory `WorkerRegistry` + Redis stream consumer → needs min-instances=1, always-on CPU and a VPC connector; that is a worse GCE |
| webhook gateway | ❌ needs Redis |
| worker | ❌ needs Redis, and ver99 needs the host `docker.sock` |

Do not extend this harness to the others.

## What it actually proves

Beyond "a service is running": on Cloud Run the container runs **as a service
account**, so `google.auth.default()` inside `llm_gateway/gcp_auth.py` resolves
to that SA instead of your local ADC user. That is the half of the keyless-auth
path a laptop cannot exercise — same code, different identity.

Verified 2026-08-04 against the live revision:

```
HTTP/2 200
x-sdlcma-backend-name: vertex-gemini-2.5-flash
x-sdlcma-cost-usd: 0.000972
usage: prompt 6 + completion 5 + reasoning 383 = total 394
```

The container reached Vertex with no API key anywhere in the deployment.

## Access model: authenticated, no public URL

Deployed **without** `--allow-unauthenticated`. This gateway has no
authentication of its own, so a leaked `*.run.app` URL would be an open door
onto the project's Vertex quota. `setup.sh` asserts both directions: with an
identity token `/health` returns 200, without one it returns 403.

Three ways to reach it:

| | How | Trade-off |
|---|---|---|
| **curl + identity token** | `-H "Authorization: Bearer $(gcloud auth print-identity-token)"` | works today, no extra install; fine for verification, not for the worker |
| **local proxy** (intended for workers) | `gcloud run services proxy llm-gateway --region us-central1 --port=9000` | worker keeps pointing at `localhost:9000`, **zero code change**; needs the `cloud-run-proxy` component (see below) |
| worker-native ID tokens | worker mints a Google ID token per call | most correct, but needs new worker code + hourly refresh — **not ver0** |

### ⚠️ `gcloud run services proxy` needs a component that apt-installed gcloud won't self-install

```
sudo apt-get install google-cloud-cli-cloud-run-proxy
```

An apt-installed Cloud SDK has the component manager disabled, so the proxy
exits immediately with "You cannot perform this action because the Google Cloud
CLI component manager is disabled" instead of installing what it needs.

### ⚠️ A busy :9000 produces a FALSE PASS

This is the more dangerous of the two, and it happened on the first run here.
The proxy failed to start (component missing), a **locally running gateway was
still listening on :9000**, and the smoke test passed — against the wrong
gateway. Nothing in the worker output distinguishes the two.

Tell them apart by the cost header, or by `RunRecord.total_cost_usd`:

* This deployment sets `cache.mode: disabled` (a Cloud Run filesystem cannot
  persist a sqlite cache — per-instance scratch, wiped on scale-to-zero).
* So **a run through Cloud Run always has a non-zero cost.** A zero cost, or
  millisecond-scale `llm_call_wallclock_ms`, means you hit a cached local
  gateway instead.

`setup.sh` step 8 warns when :9000 is already occupied.

## Resources created

| Resource | Name (default) |
|---|---|
| Artifact Registry repo | `sdlcma` in `us-central1` |
| Container image | `us-central1-docker.pkg.dev/<project>/sdlcma/llm-gateway:v1` |
| Service account | `llm-gateway-sa`, holding **only** `roles/aiplatform.user` |
| Cloud Run service | `llm-gateway`, port 9000, 512 MiB, authenticated |

The dedicated SA matters: the Compute Engine default service account carries
project **Editor**, which would let a compromised gateway rewrite the project.

Overridable via env: `PROJECT`, `REGION`, `AR_REPO`, `SERVICE`, `SA_NAME`,
`IMAGE_TAG`, `CONTAINER_PORT`.

## Gotchas encoded in setup.sh

1. **`--port 9000` is mandatory.** Cloud Run sends traffic to 8080 by default;
   the image listens on 9000. Omitting it fails the health check in a way that
   reads like an application crash loop.
2. **IAM is eventually consistent.** `add-iam-policy-binding` routinely fails
   with `Service account ... does not exist` seconds after gcloud printed
   "Created service account". Hit on the first run; setup.sh retries for 60s.
3. **The config is a template.** `configs/llm_gateway/vertex_cloudrun.yaml`
   carries `__PROJECT_ID__`; setup.sh renders `vertex_cloudrun.local.yaml`
   (gitignored) into the build context.
4. **Autoscaling is safe** because `llm_gateway/policy.py` is stateless. If a
   future circuit breaker puts state in the policy, this deployment needs
   `--max-instances=1` — pinned by
   `tests/test_gcp_cloudrun_harness.py::test_inference_policy_holds_no_mutable_state`.

## Cost

**$0.** Cloud Run's free tier (2M requests, 180k vCPU-s, 360k GiB-s per month)
dwarfs this usage and the service scales to zero when idle; the image sits
inside Artifact Registry's 0.5 GB free allowance. Only the Vertex tokens cost
anything, at roughly $0.001 per call.
