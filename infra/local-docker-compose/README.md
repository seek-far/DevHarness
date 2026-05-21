# Local docker-compose regression harness

One-command regression for `ENV=local_docker_compose`: the SDLCMA stack
containerized on the WSL host (`docker-compose.yml`) + the Windows
docker-compose GitLab joined to `sdlcma_net`.

## Prereqs
- Docker Desktop with WSL integration enabled (so `docker` works from WSL).
- Windows docker-compose GitLab up at `localhost:8080`.
- GitLab container connected to the `sdlcma_net` bridge network (the regression
  script verifies this and aborts with a fix-up command if not).
- `settings/worker_local_docker_compose.env` filled in (`GITLAB_PRIVATE_TOKEN`
  + `LLM_API_KEY`; gitignored). The image is built once via
  `docker compose --profile build build`.

## Run

```bash
bash infra/local-docker-compose/regression.sh           # full cycle, ~3-5 min
bash infra/local-docker-compose/regression.sh --no-update   # skip rebuild
bash infra/local-docker-compose/regression.sh --no-teardown # keep stack up
bash infra/local-docker-compose/regression.sh --keep-env    # don't revert ENV / webhook
bash infra/local-docker-compose/regression.sh --timeout 600 # longer smoke budget
```

Exit codes: `0` PASS · `2` FAIL · `3` TIMEOUT · `4` PRE-FLIGHT FAIL.

## Phases

1. Env check — deps, GitLab reachable, `sdlcma_net` present, GitLab joined.
2. Version detection — newest `*.py` mtime vs `dh-orchestrator:latest`
   image-created timestamp. Newer code ⇒ rebuild flagged.
3. Update — `docker compose --profile build build` (only if stale).
4. Setup — ENV swap in `settings/.env`, webhook PUT to `http://gateway:8000/webhook`,
   `docker compose up -d`, gateway healthz, inside-`sdlcma_net` path verified
   (`docker exec gitlab wget gateway:8000/healthz`).
5. Smoke — trigger a fresh pipeline on `lishu2016/order_be:main` via
   `pipeline?ref=main`, poll for new MR (`iid > baseline` + source branch
   `auto/bf/...`).
6. Restore — `docker compose down`, kill any `dh-bf-worker-*` containers the
   spawner left, revert ENV + webhook URL (unless `--keep-env`).

## Notes vs the sibling harnesses

- `webhook URL = http://gateway:8000/webhook` (not `host.docker.internal:8000`)
  because the **GitLab container** fires it — `gateway` resolves via Docker DNS
  inside `sdlcma_net`.
- bf-worker containers are spawned by `DockerWorkerSpawner` via the Docker
  socket. They're named `dh-bf-worker-<bug_id>` and outlive the compose stack
  without explicit cleanup — the teardown step removes them too.
- This harness is **mutually exclusive** with `infra/local-gitlab/` (Option 1
  systemd) and `integration_test.py`: all three claim Redis db15 and
  `gateway:stream`. The regression script does not check this — make sure
  Option 1 is torn down and integration_test is not running concurrently.
