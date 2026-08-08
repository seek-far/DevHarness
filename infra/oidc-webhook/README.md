# OIDC webhook authentication (ver0)

Turns `POST /webhook` from an unauthenticated endpoint into one that requires
a GitLab-signed OIDC token, and authorizes each request against the project
that token was minted for.

This is **additive and off by default**. Every existing harness keeps working
untouched; you opt in per deployment with `WEBHOOK_AUTH_MODE=oidc`.

## Why not `X-Gitlab-Token`

GitLab's built-in webhook auth is a shared secret compared in plaintext. It
answers *"does the caller know the secret"*, which stops anonymous abuse but
gives you nothing to authorize on: the `project.id` in the body is still a
string the caller typed, and the worker clones the URL that body names using
**our** GitLab credentials. One leaked secret means anyone can point a worker
at any project our token can reach.

A GitLab CI id_token moves the project id into a signed claim. The gateway
compares claim to payload and refuses the mismatch, so a token minted by
project A cannot trigger a run against project B — even though the token is
completely valid. That comparison is the entire reason to prefer OIDC here.

The trade is that the trigger inverts: instead of GitLab's webhook subsystem
POSTing to us, the failing CI job POSTs to us. `gitlab-ci-snippet.yml` is that
job.

## Scripted setup

```bash
OIDC_AUDIENCE=sdlcma-gateway.minus bash infra/oidc-webhook/setup.sh
bash infra/oidc-webhook/teardown.sh      # back to WEBHOOK_AUTH_MODE=none
```

`setup.sh` configures both sides and is idempotent. What it does that hand-editing does not:

- **Discovers the issuer from GitLab** rather than trusting you to type it.
  `OIDC_ISSUER` must equal the token's `iss` byte for byte, and `iss` is the
  instance's `external_url` — frequently *not* the URL you reach it on.
  Reaching a box at `http://100.115.36.114:8929` whose `external_url` is
  `http://minus:8929` is entirely normal, and copying the wrong one yields a
  401 reading "token issuer does not match", which looks like a bad token
  rather than a bad setting.
- **Pins `OIDC_JWKS_URL` only when it has to.** If the issuer's own hostname
  is not resolvable from the gateway, the issuer keeps GitLab's spelling while
  the key fetch is pointed at a reachable address. Those two settings exist
  precisely so they can disagree.
- **Refuses to invent an audience.** No default, here or in the settings
  class — GitLab mints a token for whatever `aud` a job requests, so a
  guessable audience is forgeable by construction.
- **Verifies RS256 is advertised** before enabling, and says explicitly that
  the fix is *not* to relax the pinned algorithm list.
- **Upserts the project's CI/CD variables** via the API (`SDLCMA_GATEWAY_URL`,
  `SDLCMA_AUDIENCE`), reading the token without echoing it.

It deliberately does **not** set `SDLCMA_CI_READ_TOKEN` — that is a separate,
narrower credential (`read_api`, one project), and reusing the stack's main
token would expose a full-scope credential to every job in the project.

It also does not restart the gateway, and `teardown.sh` does not touch the
project side: removing the notifier jobs would stop webhooks reaching the
gateway *at all*, a far bigger outage than turning auth off.

Useful overrides: `GITLAB_URL`, `GATEWAY_ENV`, `PROJECT_PATH`, `GATEWAY_URL`,
`SKIP_PROJECT_VARS=1` (gateway side only).

## Gateway side

```bash
# gateway/gateway_<env>.env
WEBHOOK_AUTH_MODE=oidc
OIDC_ISSUER=https://gitlab.example.com          # must equal the token's `iss`
OIDC_AUDIENCE=https://sdlcma.example.com        # must equal `aud` in the snippet
# optional
OIDC_JWKS_URL=                                  # default: {issuer}/oauth/discovery/keys
OIDC_JWKS_CACHE_SECONDS=300
OIDC_JWKS_MIN_REFRESH_SECONDS=60
OIDC_LEEWAY_SECONDS=30
```

`OIDC_AUDIENCE` has no default on purpose. GitLab mints a token for whatever
`aud` a `.gitlab-ci.yml` asks for, so a guessable audience would let any
project on the instance forge a trigger. Use a value specific to this
deployment.

Missing issuer or audience while the mode is `oidc` is a **fatal
misconfiguration** (HTTP 500 on every webhook), never a fallback to
unauthenticated. Same for an unrecognised mode value.

## Project side

Set three CI/CD variables, then `include:` the snippet:

| Variable | Masked | Purpose |
|---|---|---|
| `SDLCMA_GATEWAY_URL` | no | e.g. `https://sdlcma.example.com` |
| `SDLCMA_AUDIENCE` | no | must equal the gateway's `OIDC_AUDIENCE` |
| `SDLCMA_CI_READ_TOKEN` | yes | `read_api`, this project only — see below |

```yaml
include:
  - project: 'your-group/sdlcma-ci'
    file: '/gitlab-ci-snippet.yml'
```

### Why a read_api token is still needed

The worker fetches the *failing job's* trace by id. A `.post`-stage notifier
cannot read a sibling job's id from predefined variables — `CI_JOB_ID` is its
own — so the snippet queries the pipeline's failed jobs. Without the token it
still fires, but hands the worker its own job id, and the worker ends up
diagnosing a `curl` invocation instead of the real failure. The snippet logs a
warning in that case.

This is the one long-lived credential the OIDC path does **not** remove. Scope
it to `read_api` on the single project.

## What is verified

| Check | Failure |
|---|---|
| RS256 signature against the issuer's JWKS | 401 |
| `iss` equals `OIDC_ISSUER` | 401 |
| `aud` equals `OIDC_AUDIENCE` | 401 |
| `exp` / `nbf` / `iat` within leeway | 401 |
| `project_id` claim present | 401 |
| **claim `project_id` == payload `project.id`** | **403** |

401 vs 403 is meaningful: 401 means "you did not prove who you are", 403 means
"you did, and you may not do that". Both are counted separately in
`sdlcma_webhook_auth_rejected_total{reason}`.

The algorithm list is hardcoded to RS256 and is not configurable — widening it
is how `alg: none` and RS256→HS256 confusion attacks land. Both are pinned by
tests.

A rejected request is never written to `gateway:stream`, so no worker is
spawned and no LLM budget is spent on it. That is the property being
defended.

## Rollout

The two sides are independent, so cut over without downtime:

1. Deploy the snippet to one project while the gateway is still
   `WEBHOOK_AUTH_MODE=none`. Both GitLab's webhook and the snippet now fire;
   duplicate triggers are absorbed by the existing idempotency contract
   (deterministic branch name → three-state push → MR lookup-then-create).
2. Confirm `phase_marker phase=webhook_auth` never appears (mode is off) but
   the snippet's POSTs return 200.
3. Flip the gateway to `oidc`. GitLab's own webhook deliveries now 401 — watch
   `sdlcma_webhook_auth_rejected_total{reason="missing_token"}` climb, which
   is how you find projects you have not migrated yet.
4. Remove the project's GitLab webhook once its counter stops moving.

## Observability

```
phase_marker phase=webhook_auth result=accept project_id=<id> sub=<claim sub>
phase_marker phase=webhook_auth result=reject reason=<reason> status=<code> detail=<msg>
```

The reject line is the audit record for a trigger that never became a run —
it has no `bug_id`, so without it a refused request leaves no trace at all.
`sub` on the accept line is GitLab's
`project_path:<path>:ref_type:branch:ref:<branch>`, which is the closest
thing to an actor identity available before the orchestrator mints a bug_id.

## Tests

`tests/test_gateway_webhook_oidc.py` — 31 cases covering the no-op default,
each claim check, both algorithm-confusion attacks, the int-vs-string project
id normalisation, fail-closed misconfiguration, and the JWKS refetch throttle.
