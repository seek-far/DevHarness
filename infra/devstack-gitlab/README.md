# devstack-gitlab test harness

Reproducible setup for testing **GitLab mode** with the SDLCMA stack running
**inside a DevStack OpenStack VM** while GitLab is a docker-compose container
on the Windows host ("Option 2"). This exists so the many environment changes
required are codified, not tribal knowledge.

```
GitLab(container, Win :8080/:2222, external_url gitlab.local)
  └─ webhook → host.docker.internal:8000 → Windows → WSL host:8000
       └─ [sdlcma-relay8000.service]  socat → VM 172.24.4.71:8000
            └─ gateway(0.0.0.0:8000) → redis → orchestrator → worker  (all in VM)
                 └─ clone/commit/push → http://localhost:8080  (VM)
                      └─ [gitlab-fwd.service] socat → 192.168.128.1:8080 → real GitLab
```

## Use

```bash
bash infra/devstack-gitlab/setup.sh        # idempotent; brings everything up
# then in GitLab UI: project → Webhooks → http://host.docker.internal:8000/webhook
#                     (Pipeline + Job events), trigger a failing pipeline
bash infra/devstack-gitlab/teardown.sh             # stop services, keep VMs
STOP_VMS=1 bash infra/devstack-gitlab/teardown.sh  # also power off VMs (free RAM)
FULL=1     bash infra/devstack-gitlab/teardown.sh  # also drop unit files + secgroup rule
```

## Test scripts

`setup.sh` only builds the environment. The two scenarios are tested by:

```bash
# devstack EVALUATION — self-contained, no GitLab, deterministic.
# Runs the sweep in the VM, pulls results back, prints the report. Exit 0 = ok.
bash infra/devstack-gitlab/eval-in-vm.sh
CONFIG=configs/memory_vs_baseline.json bash infra/devstack-gitlab/eval-in-vm.sh

# devstack+GITLAB — end-to-end smoke. Watches one full run and asserts
# journal outcome="fixed" + an opened MR (never a commit to main).
# Trigger is external: push a failing commit / re-run the pipeline, OR:
RETRY_PIPELINE=10 bash infra/devstack-gitlab/gitlab-smoke.sh
bash infra/devstack-gitlab/gitlab-smoke.sh        # manual trigger, then it watches
```

`gitlab-smoke.sh` exits 0 only on `(outcome=fixed, review_status=opened)`;
non-zero on a different terminal record (2) or timeout (3). It requires the
project's `main` to actually reproduce the bug (else the worker finds no diff,
nothing is pushed, no fix-branch CI fires — see Issue #5 in project memory).

Typical reboot recovery flow:
`setup.sh` → `eval-in-vm.sh` (fast confidence check) → re-introduce the bug on
the GitLab project → `gitlab-smoke.sh`.

Tunables are env vars at the top of `setup.sh` (VM IP, Windows gateway, ssh
key, project dir, …); defaults match the dev machine this was built on.

## Why these specific steps (the non-obvious gotchas)

1. **Zero code change via VM `localhost:8080` forward.** `local_multi_process`
   hardcodes the repo host to `localhost:8080`; `gitlab-fwd.service` makes the
   VM's `localhost:8080` transparently be the real GitLab, so neither the
   provider rewrite nor `GITLAB_API` need editing.
2. **OpenStack secgroup must open tcp/8000.** The `infra/terraform/modules/iaas`
   group only opens 22/6443/10250/30000-32767 — the gateway port is added by
   `setup.sh`. (Lost if that terraform root is re-applied.)
3. **`python-is-python3`.** `apply_change_and_test` shells out to bare
   `python -m venv`; Ubuntu only has `python3`.
4. **git identity via `--system`, not `--global`.** The worker runs git via
   `subprocess(env=os.environ.copy())` under systemd, which has no `HOME`, so
   `/root/.gitconfig` is never read; `/etc/gitconfig` is.
5. **Persist services with systemd, never `nohup &` over ssh** (and the WSL
   reboot network recovery — br-ex + egress NAT — must be done first; see
   `infra/terraform/iaas-openstack` and project memory).

## What is NOT here

The actual product fix that made the CI-result feedback loop work —
`orchestrator/parser.py` recognising the real `auto/bf/{bug_id}-{sha8}`
fix-branch name plus `tests/test_parse_branch.py` — is in the repo proper,
because it is a real bug fix, not test scaffolding. It is additive: the legacy
`auto/bug_..-patch_..` shape (used by `integration_test.py`) is unchanged.

End state of a successful run: journal record `outcome="fixed"`, an MR opened
from `auto/bf/<bug_id>-<sha8>` into `main`. The agent proposes an MR for
review; it never commits to `main`.
