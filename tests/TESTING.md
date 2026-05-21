# Testing runbook

Every testing surface in this repo, how to run it, and what "pass" means.
Ordered cheapest/fastest → most integrated.

> Location note: this file lives at `tests/TESTING.md` on purpose. `.gitignore`
> ignores root-level `*.md` (except `README.md`) **and** the whole `docs/`
> tree (those are local-only deep contracts), so a *tracked* testing doc has
> to sit in a subdirectory. `README.md` → `## Test Utilities` links here.

Python invocation convention (whole project): activate the Linux venv and go
through `uv run`, never call the venv interpreter directly:

```bash
source .venv-linux/bin/activate && uv run python ...
source .venv-linux/bin/activate && uv run pytest tests/
```

`apply_change_and_test` shells out to bare `python -m venv`, so `python` must
be on `PATH` — `uv run` / an activated venv guarantees that.

---

## 1. Unit tests (`tests/`)

```bash
source .venv-linux/bin/activate && uv run pytest tests/ -q
```

Pure-Python, no Redis/GitLab/network. Pass = all green. Current baseline:
**396 passed** (~28s). This is the regression floor — run it for any
non-trivial change before reporting done.

Notable pinned contracts (don't break silently):
- `tests/test_gitlab_saas_env.py` — `gitlab_saas` does NO host rewrite +
  `https://oauth2:<token>@…` clone; asserts the `local_*` env branches stay
  byte-identical.
- `tests/test_parse_branch.py` — orchestrator parser recognises both the
  legacy `auto/bug_..-patch_..` and the real
  `auto/bf/{bug_id}-{base_commit[:8]}` fix-branch names.

## 2. Integration test (full pipeline, in-process)

```bash
source .venv-linux/bin/activate && \
  uv run python integration_test.py [--redis-url redis://...] \
  [--bug-id BUG-IT-1] [--config configs/baseline_last_commit.json]
```

End-to-end gateway → orchestrator → worker via FastAPI `TestClient` with
short timeouts. `--config` sets `BF_AGENT_CONFIG` for the spawned worker; with
`agent_ref` the worker re-executes from a pinned detached worktree.

> **PRECONDITION — mutually exclusive with the Option-1 systemd stack.** Stop
> it first: `bash infra/local-gitlab/teardown.sh`. Both run env
> `local_multi_process` → Redis **db15**, stream `gateway:stream`, consumer
> group **`orchestrator-group-mp`**. A consumer group delivers each entry to
> exactly one consumer, so two orchestrators (the in-process one here + the
> systemd `sdlcma-local-orchestrator`) **race** for every webhook. Under
> `local_multi_process` the worker pushes the fix branch to the *real* GitLab,
> which runs the fix-branch CI and POSTs a real pipeline webhook to the
> systemd gateway on `:8000` → the shared stream. If the systemd orchestrator
> wins that entry it logs `[Router] no active worker for bug_id=…` and drops
> it → the in-process worker's `wait_ci_result` times out (300s) →
> `handle_failure` → **no MR**, flakily. After teardown the in-process
> orchestrator is the sole group consumer and the run is deterministic
> (verified: passes, creates an MR). The synthetic Step E
> (`auto/bug_{bug_id}-patch_…`, legacy shape) is vestigial in this env — it
> arrives after the worker has exited; the real GitLab fix-branch webhook is
> what actually drives success. The legacy Step E shape is also why the
> real-fix-branch parser bug (devstack Issue #4) was invisible here and only
> caught by a real GitLab run. Never run `gitlab-smoke.sh` and
> `integration_test.py` against the same Redis concurrently.

## 3. Evaluation sweeps (comparison, not pass/fail)

```bash
source .venv-linux/bin/activate
uv run python -m evaluation.cli list-fixtures
uv run python -m evaluation.cli run            # sweep agents × fixtures
uv run python -m evaluation.cli report <run_id>
```

Compares bug-fix approaches over curated fixtures; output in
`evaluation/runs/<run_id>/`. **Reproducibility-critical:** eval is only valid
with `BF_CHECKPOINT_BACKEND=none` — a shared `bug_id`-keyed sqlite checkpoint
otherwise resumes a prior run across cells/specs/processes and contaminates
results. Repeated/long sweeps run as parallel staggered background processes,
never a sequential loop.

## 4. One-command regression (`infra/*/regression.sh` + `tests/integration_test_wrapper.sh`)

Each deployment has a single-entrypoint regression script that does
**env check → version detection → update if stale → setup → smoke → restore**
with timestamped progress lines and standard exit codes (0 PASS / 2 FAIL /
3 TIMEOUT / 4 PRE-FLIGHT FAIL). Common flags:

- `--timeout N`     — smoke wall-clock budget
- `--no-update`     — skip rebuild/push step
- `--no-teardown`   — leave stack running after smoke
- `--keep-env`      — don't revert `settings/.env` or webhook URL

| Script | What it covers | Default smoke target |
|---|---|---|
| `tests/integration_test_wrapper.sh` | `integration_test.py` (FastAPI TestClient, isolated Redis db=15) | in-process |
| `infra/local-gitlab/regression.sh` | Option 1 (systemd, `local_multi_process`) | Windows docker-compose GitLab @ `localhost:8080` |
| `infra/local-docker-compose/regression.sh` | Containerized stack (`local_docker_compose`) | Windows docker-compose GitLab on `sdlcma_net` |
| `infra/public-host/regression.sh` | Stage 2 on IONOS public host, gitlab.com via cloudflared | gitlab.com `lishu20161/order_be` |
| `infra/aws-ecs/regression.sh` | AWS ECS on EC2, gitlab.com via cloudflared | gitlab.com `lishu20161/order_be` |

Each script's header comment lists its specific knobs (e.g. `aws-ecs` adds
`--teardown=full` for `delete-stack`; `public-host` honours `HOST`/`SSH_KEY`
env overrides). The scripts are idempotent: re-running picks up the right
state automatically.

The lower-level `setup.sh` / `gitlab-smoke.sh` / `teardown.sh` /
`deploy-images.sh` building blocks remain for manual / partial use.

## 5. Real-host GitLab-mode smokes (`infra/*/gitlab-smoke.sh`) — manual building blocks

Three idempotent harnesses run the *whole* stack against a *real* GitLab and
assert one full run reaches `outcome="fixed"` + an MR opened from
`auto/bf/<bug_id>-<sha8>` into `main` (the agent never commits to main). Deep
setup contracts: `docs/deployment.md` (local-only) + each
`infra/*/README.md`.

| Harness | Topology | Agent env |
|---|---|---|
| `infra/local-gitlab/` (Option 1) | stack on the WSL host, GitLab=docker-compose on Windows | `local_multi_process` |
| `infra/devstack-gitlab/` (Option 2) | stack inside a DevStack OpenStack VM, same GitLab | `local_multi_process` |
| `docker-compose.yml` (cloud Stage 1) | **containerized** stack, GitLab=docker-compose on Windows | `local_docker_compose_http` |
| `infra/public-host/` (Phase 0.5) | **containerized** stack on a public-IP host, gitlab.com + cloudflared | `gitlab_saas` |
| `infra/aws-gitlab/` | single host, gitlab.com (SaaS) | `gitlab_saas` |
| `infra/aws-ecs/` | ECS on EC2 t3.micro (services as one host-net task; per-bug `bf-worker` task via `ecs:RunTask`), gitlab.com + cloudflared | `gitlab_saas` + `WORKER_SPAWNER=ecs` |

Each: `bash infra/<x>/setup.sh` → trigger a failing pipeline → `bash
infra/<x>/gitlab-smoke.sh` → `bash infra/<x>/teardown.sh`. Exit 0 =
`(fixed, opened)` within `TIMEOUT`; non-zero on timeout/wrong outcome.

### 4z. Containerized Stage-1 (`local_docker_compose_http`)

Cloud-track Stage 1: the *whole* stack containerized, proving the
containerization itself locally at $0 before pointing the same compose stack
at gitlab.com (Stage 2 = `gitlab_saas` + cloudflared; see
`/mnt/d/PL/sdlcma/cloud-gitlab-plan.md`).

```bash
bash infra/local-gitlab/teardown.sh              # MUST: mutually exclusive — see below
docker network create sdlcma_net 2>/dev/null || true
docker network connect sdlcma_net gitlab         # GitLab container resolvable as `gitlab`
docker compose --profile build build             # builds gateway/orchestrator/worker
docker compose up -d                             # ENV=local_docker_compose_http
# repoint the project webhook → http://gateway:8000/webhook (pipeline events)
# trigger a failing order_be pipeline; then verify on GitLab (source of truth):
#   MR opened auto/bf/<bug>-<sha8> → main  +  that fix-branch pipeline = success
docker compose down                              # teardown
```

> **PRECONDITION — mutually exclusive (same lesson as §2).** The orchestrator
> uses Redis group `orchestrator-group-mp`. Any *other* orchestrator on a
> reachable Redis (the Option-1 systemd stack, a stray `integration_test.py`)
> races for webhooks. The compose Redis is internal to `sdlcma_net` (no host
> publish) so it is isolated **from host Redis**, but still stop the Option-1
> systemd stack first (`bash infra/local-gitlab/teardown.sh`) — only one
> stack may own the `order_be` webhook + answer it.

Two real packaging gaps Stage 1 caught locally (now fixed; both were
invisible to host/systemd runs because the host venv/PYTHONPATH masked them):
- `inspection/` not in the worker image (`graph/nodes/code_review.py` imports
  it) → `Dockerfile.bf-worker` `COPY inspection/`.
- worker image lacks `langgraph-checkpoint-sqlite`, checkpointer defaults to
  `sqlite` → `DockerWorkerSpawner` injects `BF_CHECKPOINT_BACKEND=none`
  (ephemeral container; also dodges the §2 contamination hazard).

Verified 2026-05-19 (GitLab API): MR **!39** opened
`auto/bf/2026_05_19-17_56_33_3-62d30b75`→`main`, fix-branch pipeline **#107
success**. Always pair the journal/worker-log with the GitLab-API cross-check
(`merge_requests?source_branch=…` + `pipelines?ref=…`).

### 4z-2. Containerized Stage-2 (`gitlab_saas` + cloudflared, no public IP)

Same compose stack → gitlab.com. Override adds cloudflared and flips ENV:

```bash
docker compose -f docker-compose.yml -f docker-compose.gitlab_saas.yml \
  --profile build build
docker compose -f docker-compose.yml -f docker-compose.gitlab_saas.yml up -d
docker compose -f docker-compose.yml -f docker-compose.gitlab_saas.yml \
  logs cloudflared            # → https://<rand>.trycloudflare.com
# set the gitlab.com project webhook to  <that-url>/webhook  (pipeline events)
# trigger a failing pipeline; verify on gitlab.com:
#   merge_requests?source_branch=auto/bf/<bug>-<sha8>  +  that pipeline = success
```

- Webhook URL is the cloudflared `https://<rand>.trycloudflare.com/webhook`,
  **not** an ip:port (no public IP; cloudflared dials out). Ephemeral —
  changes on every cloudflared restart; re-set the gitlab.com webhook.
- `orchestrator_gitlab_saas.env` sets `WORKER_SPAWNER=docker` so the
  gitlab.com env still spawns worker containers (decoupled from env;
  `tests/test_worker_spawner_selection.py` pins empty/`auto` = byte-identical
  historical mapping).
- Auth method is unchanged vs Stage 1 — same access token, same
  `PRIVATE-TOKEN` REST header, no SSH; only the git URL is HTTPS/public
  (`https://oauth2:<token>@gitlab.com/...`).
- Token lives only in gitignored `settings/{worker,orchestrator}_gitlab_saas.
  env` (+ baked into the local `dh-bf-worker` image) — short-lived, revoke
  after. Same mutual-exclusion rule as §2/§4z.

Verified 2026-05-19 (gitlab.com API): `lishu20161/order_be` →
**MR !1 opened** `auto/bf/2026_05_19-19_49_42_9-5cf79cc5`→`main`, fix-branch
CI success — both webhooks round-tripped through the cloudflared tunnel.

- To teardown, docker compose down the two projects:
  docker-compose.yml -f docker-compose.gitlab_saas.yml

### 4z-3. Public-host migration (`infra/public-host/`, Phase 0.5)

Same containerized stack moved onto a real public-IP host (verified on
`82.165.48.174`, Ubuntu 24.04, 2 vCPU / 1.8 GB, IONOS). Driven from the WSL
dev box; SSH key + root account.

```bash
ssh -i ~/.ssh/sales_deploy root@82.165.48.174 \
  'bash /root/sales_02-deploy/teardown.sh'    # free RAM: stop sales-retro
bash infra/public-host/setup.sh               # docker, 4G swap, ship images,
                                              # compose up, print tunnel URL
# read the printed https://<rand>.trycloudflare.com URL and set it as the
# gitlab.com project webhook (Pipeline events, SSL ON — valid cert)
# trigger a failing pipeline; verify on gitlab.com API:
#   merge_requests?source_branch=auto/bf/<bug>-<sha8>  +  that pipeline=success
bash infra/public-host/teardown.sh            # compose down + swapoff+rm
```

> **PRECONDITION — co-tenant.** The host also runs the live `sales-retro`
> app (Caddy → 127.0.0.1:8765) from a separate repo (`/mnt/d/my_git/
> sales_02`). 2 GB RAM is tight, so always run `sales_02/deploy/teardown.sh`
> *first* (stop + `systemctl disable`; reboot-OFF). Restore with
> `sales_02/deploy/setup.sh` after. Caddy is left running throughout.
>
> **Reboot-OFF semantic (project-wide).** Compose services carry no
> `restart:` policy and `teardown.sh` removes the swapfile too, so a host
> reboot brings *nothing* SDLCMA back — only `setup.sh` does.

Real finding worth pinning: the host's **upstream IONOS provider firewall
drops inbound `:8000`** (only 22/80/443 reach the OS — verified via the host
reaching its own public IP `:8000` 200 via hairpin while the external
internet times out). So cloudflared is **mandatory here** even with a public
IP. `docker-compose.public-host.yml` includes the cloudflared service for
this reason; `setup.sh` prints the trycloudflare URL after up. (A Caddy
reverse-proxy on the already-open `:443` is a possible later nicety but
would edit the live Caddyfile — deliberately not done.)

Verified 2026-05-20 (gitlab.com API): `lishu20161/order_be` retry →
**MR !5 opened** `auto/bf/2026_05_19-22_06_22_4-5cf79cc5`→`main`, fix-branch
CI pipeline **2538661005 success** — webhook (initial + validation) both
round-tripped through the cloudflared tunnel.

### 4z-4. AWS ECS (`infra/aws-ecs/`)

Managed-cluster cloud variant. Same auth as 4z-3 (`gitlab_saas` →
HTTPS+`oauth2:<token>`, cloudflared for inbound). The only delta is
`WORKER_SPAWNER=ecs` → workers run as ECS tasks (`ecs:RunTask`) on the same
t3.micro EC2 instance, instead of as Docker containers off `docker.sock`.
No new `ENV` value; the worker reuses the existing `gitlab_saas`
GitLab-provider branch unchanged. The services task uses `NetworkMode: host`
so the 4 long-running containers share `localhost`; the bf-worker task uses
`awsvpc`, and the orchestrator passes the EC2 host's private IPv4 in
`ECS_WORKER_REDIS_URL` because the worker's `localhost` is its own ENI.

```bash
# 1) infra (idempotent CloudFormation; auto-discovers default VPC+subnets)
KEY_NAME=sdlcma-key \
GITLAB_TOKEN=glpat-xxx \
LLM_API_KEY=sk-xxx \
bash infra/aws-ecs/create-stack.sh

# 2) build & push the 3 images to ECR
bash infra/aws-ecs/deploy-images.sh

# 3) scale the services task up (created at DesiredCount=0 so step 2 runs first)
aws ecs update-service --cluster sdlcma-cluster --service sdlcma-services \
    --desired-count 1 --region us-east-1

# 4) grab the cloudflared URL (set as gitlab.com webhook, Pipeline events)
aws logs tail /sdlcma/services --filter trycloudflare --region us-east-1

# 5) smoke: retry a failing pipeline, watch CloudWatch, verify MR
PROJECT_PATH=lishu20161/order_be GITLAB_TOKEN=glpat-xxx \
bash infra/aws-ecs/gitlab-smoke.sh

# 6) teardown
bash infra/aws-ecs/delete-stack.sh                # keep ECR repos
DELETE_ECR=1 bash infra/aws-ecs/delete-stack.sh   # full cleanup
```

Unit coverage: `tests/test_ecs_spawner.py` pins the RunTask shape (launch
type, awsvpc config, the **mandatory** `command=["--bug-id", bug_id]`
override — without it the worker entrypoint runs with no args and hangs),
the `ENV=gitlab_saas` worker env (not `"ecs"`), idempotency, restart, the
host-mode network branch (no `networkConfiguration` — ECS rejects it for
bridge/host), and the `EcsTaskProxy.reload_status` race
(empty-`tasks`-without-MISSING leaves returncode=None so HealthMonitor
doesn't unregister a live worker).
`tests/test_worker_spawner_selection.py` pins that ECS is reachable *only*
via explicit `WORKER_SPAWNER=ecs` (no auto-mapping from an `ENV=ecs`,
which doesn't exist). Last proven run: 2026-05-21, MR !6 on
`lishu20161/order_be`, ~65 s trigger→MR (eu-north-1).

### 4a. Option-1 end-to-end — exact procedure & last proven run

This is the cheapest full GitLab-mode test (no VM); it rehearses the
public-IP-host path.

**Preconditions** (`setup.sh` verifies, doesn't create): Windows
docker-compose GitLab up (`external_url http://gitlab.local`, published
`:8080→80`/`:2222→22`); Redis on `localhost:6379`; `.venv-linux` with deps;
`settings/.env=local_multi_process` and the worker/orchestrator env files
filled; target project's `main` actually reproduces a bug (else the LLM patch
yields no diff → no push → no MR — devstack Issue #5).

**Steps:**

1. `bash infra/local-gitlab/setup.sh` — stops the devstack Option-2
   `sdlcma-relay8000.service` (it squats on `:8000` and would misroute
   webhooks to the now-down VM), starts `sdlcma-local-gateway` +
   `sdlcma-local-orchestrator` systemd units (run as the dev user with
   `HOME`+venv on `PATH`), checks Redis/GitLab reachable, prints the project
   webhook (must be `http://host.docker.internal:8000/webhook`, Pipeline
   events).
2. Trigger a failing pipeline on `lishu2016/order_be`. Either push a buggy
   commit, or — what the smoke does — `RETRY_PIPELINE=<id>
   bash infra/local-gitlab/gitlab-smoke.sh` to `POST .../pipelines/<id>/retry`
   an existing failed pipeline whose ref still has the bug.
3. Chain (no stubs): GitLab fires the pipeline webhook →
   `host.docker.internal:8000` → Docker Desktop → Windows → WSL2
   localhostForwarding → host gateway → `gateway:stream` → orchestrator parser
   (`status=failed` → `BugReportedEvent`) → spawns worker → worker clones via
   `http://user:token@localhost:8080` (`gitlab.local→localhost:8080` rewrite,
   zero code change), LLM fix, branch `auto/bf/<bug_id>-<sha8>`, `python -m
   venv`+pytest, commit, push → GitLab runs fix-branch CI → success webhook
   round-trips → parser `ValidationStatusEvent` → router →
   worker `wait_ci_result` → `create_mr` → MR opened, journal
   `outcome=fixed`.
4. `gitlab-smoke.sh` asserts: a NEW `evaluation/journal/` record (count >
   baseline) whose `record.json` has `outcome=="fixed"` AND
   `review_status=="opened"` → exit 0.
5. **Independent cross-check (do this; don't trust the journal alone)** —
   the journal is the worker's own self-report. Verify on GitLab via API:
   ```bash
   TOK=$(grep -oE 'GITLAB_PRIVATE_TOKEN=.*' settings/worker_local_multi_process.env | cut -d= -f2)
   PID=$(.venv-linux/bin/python -c "import urllib.parse;print(urllib.parse.quote('lishu2016/order_be',safe=''))")
   B=http://localhost:8080/api/v4/projects/$PID
   curl -sS -H "PRIVATE-TOKEN: $TOK" "$B/merge_requests/<iid>"            # state=opened, src/target
   curl -sS -H "PRIVATE-TOKEN: $TOK" "$B/pipelines?ref=<fix-branch>"      # fix-branch CI = success
   ```
6. `bash infra/local-gitlab/teardown.sh` (add `RESTORE_RELAY=1` to re-enable
   the devstack Option-2 relay; Option-1 and Option-2 share `:8000` and are
   mutually exclusive).

**Last proven run — 2026-05-19 (cross-verified against GitLab API):**

| Item | Value |
|---|---|
| Trigger | retried `lishu2016/order_be` pipeline #99 (`main` `62d30b75` "artificial bug 260516", went `failed`) |
| Worker | `bug_id=2026_05_19-15_41_14_3`, `local_multi_process`, iterations=0 (one-shot) |
| Fix branch / commit | `auto/bf/2026_05_19-15_41_14_3-62d30b75` / `df2265ea` |
| Fix-branch CI | pipeline **#102 success** (GitLab API) |
| MR | **!35 opened**, source fix branch → target `main`, never committed to main (GitLab API: `state=opened`) |
| Journal | `outcome=fixed`, `review_status=opened` |
| Regression | unit suite 396 passed (harness was infra+docs only) |

**Coverage boundary (be honest about this):** the trigger above was an API
*retry* of an existing failed pipeline, not a fresh developer `git push` of a
new buggy commit. The webhook payload shape, parser, worker, fix-branch CI and
MR path are all genuinely exercised and identical to a real CI failure; what a
retry does NOT cover is a brand-new sha + push origination. Also: single
project, single bug, one run, LLM happened to one-shot it — not a
repeat/stress/multi-fixture test (that is the evaluation sweep, §3). For the
hardest evidence, repeat §4a step 2 by pushing a new buggy commit instead of
retrying.

---

## What "tested" means here

- §1–§2 are deterministic and gate every change.
- §3 is a comparison harness, not a pass/fail gate.
- §4 proves the real integration (webhook → fix → MR) against a real GitLab;
  always pair the journal assertion with the §4a-step-5 GitLab-API
  cross-check before claiming end-to-end.
