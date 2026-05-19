# Option 1 — SDLCMA GitLab mode on the WSL host

The whole stack (gateway + orchestrator + per-bug worker) runs **directly on
the WSL host** as the developer user; GitLab is the docker-compose container
on the Windows host (`external_url http://gitlab.local`, published
`:8080->80`, `:2222->22`). Agent env: **`local_multi_process`**.

This is the no-VM sibling of [`infra/devstack-gitlab/`](../devstack-gitlab/)
(Option 2, stack inside a DevStack OpenStack VM). Same GitLab container, same
zero-code-change trick, far less plumbing — its purpose is to **rehearse the
public-IP-host path** cheaply before any cloud spend (see
`docs/deployment.md` and `/mnt/d/PL/sdlcma/cloud-gitlab-plan.md`).

## Why `local_multi_process` (not `local_docker_compose`)

`local_multi_process` clones/pushes over `http://user:token@localhost:8080`
(HTTP token, **no SSH**). That is exactly the model the AWS-bound
`gitlab_saas` env mirrors over HTTPS (`https://oauth2:<token>@gitlab.com`), so
this rehearsal is faithful to where the cloud path goes.
`local_docker_compose` rewrites `gitlab.local` to the **`gitlab` container
hostname** and uses SSH — only resolvable inside the compose network, and the
opposite auth model. Wrong for a host-side stack and for the cloud direction.

## What Option 1 drops vs Option 2

Running on the host (not a root-owned VM unit) as the dev user removes every
DevStack/VM workaround:

| Option 2 (VM) needed | Option 1 (host) |
|---|---|
| DevStack net recovery, OpenStack VM, secgroup tcp/8000 | — (no VM) |
| rsync repo to the VM | run in place |
| socat `localhost:8080` → GitLab | — Docker Desktop WSL2 localhostForwarding already makes Windows-published `:8080` reachable as `localhost:8080`, so the hardcoded `gitlab.local→localhost:8080` rewrite needs no code change |
| host webhook relay `:8000` → VM | the gateway **is** the `:8000` listener |
| `python-is-python3` (gotcha #2) | units run with the venv on `PATH` |
| git identity `--system` (gotcha #3) | `User=`+`HOME=` ⇒ the dev's `--global` git identity applies |

The one non-obvious host change it **does** codify: the devstack Option-2
`sdlcma-relay8000.service` (socat WSL `:8000` → the now-down VM) squats on
`:8000` and would misroute every webhook. `setup.sh` stops+disables it;
`teardown.sh RESTORE_RELAY=1` puts it back.

## Webhook landing path

GitLab container → `http://host.docker.internal:8000/webhook` → Docker Desktop
resolves `host.docker.internal` to Windows → WSL2 localhostForwarding → the
WSL-host gateway on `:8000`. The `lishu2016/order_be` webhook is already set
to this URL with Pipeline events on (Job events optional).

## Usage

```bash
bash infra/local-gitlab/setup.sh        # stop relay, start host units, verify
# trigger a FAILING pipeline on a project whose main reproduces a bug
bash infra/local-gitlab/gitlab-smoke.sh # assert outcome=fixed + opened MR
bash infra/local-gitlab/teardown.sh     # stop units (RESTORE_RELAY=1 to undo)
```

Idempotent; safe to re-run. Watch a live run with
`sudo journalctl -u sdlcma-local-orchestrator -f`.

## Preconditions (one-time, not created by setup.sh)

- Windows docker-compose GitLab up (standard config above).
- Redis reachable at `localhost:6379` (the user's redis container is fine).
- `.venv-linux` present with deps installed.
- `settings/.env` = `local_multi_process`; `worker_/orchestrator_` env files
  filled (GitLab token, LLM key).
- Target project's `main` actually reproduces a bug — otherwise the LLM patch
  yields no diff → no push → no fix-branch CI → no MR (devstack memory
  Issue #5).
