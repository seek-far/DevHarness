# aws-gitlab harness (gitlab.com / pre-AWS rehearsal)

GitLab mode against **gitlab.com (SaaS)** on a single generic public Ubuntu
host. This is the AWS-bound path; it is deliberately the `devstack-gitlab`
harness with **all WSL/OpenStack plumbing removed** because gitlab.com is
public:

```
gitlab.com  ──webhook──▶  cloudflared free tunnel  ──▶  gateway:8000 (host)
                                                          │
   worker ──HTTPS clone/push──▶ https://oauth2:<token>@gitlab.com/...
   redis + gateway + orchestrator + worker  = systemd on ONE host
```

No `gitlab-fwd`, no host relay, no `br-ex`/NAT, no secgroup-8000, no
`host.docker.internal`. The only new things vs Option 2 are the `gitlab_saas`
env (HTTPS + token, no host rewrite — `orchestrator/parser.py` and the
provider already handle it, see `tests/test_gitlab_saas_env.py`) and a
cloudflared tunnel for inbound webhooks (zero open ports, $0).

## Phasing (cost-optimised)

| Phase | Where | Cost | Command |
|---|---|---|---|
| 0.5 rehearsal | your existing public host | $0 | `HOST=… bash provision.sh` then the tests below |
| 2 eval on AWS | Spot t3.micro (+swap), stop when idle | ≈$0 | same `provision.sh` on the EC2, `eval-on-host.sh` |
| 3 gitlab on AWS | same EC2 | ≈$0 | same `provision.sh`, `gitlab-smoke.sh` |

Because Phase 0.5 proves the only two AWS-new variables (gitlab.com auth +
public webhook ingress) for free, the AWS phases are a near-instant, near-zero
stamp-out of the *identical* script + test scripts.

## Use

```bash
HOST=ubuntu@<ip> SSH_KEY=~/.ssh/<key> bash infra/aws-gitlab/provision.sh
# fill the gitlab.com token in settings/worker_gitlab_saas.env on the host,
# set the printed cloudflared URL as the project's webhook (+/webhook),
# then:
HOST=… bash infra/aws-gitlab/eval-on-host.sh                       # step 1 (no GitLab)
HOST=… PROJECT_PATH=grp/buggy bash infra/aws-gitlab/gitlab-smoke.sh # step 2
HOST=… bash infra/aws-gitlab/teardown.sh                            # stop
```

Requires a gitlab.com project whose `main` actually reproduces a bug (else no
diff → no push → no fix-branch CI; see Issue #5 in project memory) and a
short-lived Project/Group access token (scopes: `api`, `write_repository`).

## Cost guardrails for the AWS phases (Phase 1 first)

New account: AWS Budgets $1 + zero-spend budget + Free-Tier alerts; non-root
IAM user; **us-east-1**; single AZ; gp3 8GB. **Never create** NAT Gateway,
ALB, EKS, or leave an Elastic IP. `stop`/`terraform destroy` between sessions
(stopped EC2 = only cents of EBS). eval box = Spot (sweep is idempotent).
1 GB free-tier RAM is tight → add 2–4 GB swap (the provisioning assumes the
~2 GB-class box we already proved Option 2 on).
