# Infrastructure (k8s + Terraform)

Local k8s, Helm chart, and Terraform setup for SDLCMA. Separate from app code
(`bf_worker/`, `gateway/`, `orchestrator/`) — this is deploy infrastructure.

Roadmap follows `1a.1 → 1a.6` (see top-level conversation / task list):

- `1a.1` — manual kind deploy (this stage)
- `1a.2` — full native manifests (Deployment / Service / ConfigMap / Secret / PVC)
- `1a.3` — spawner → k8s Job refactor
- `1a.4` — Helm chart
- `1a.5` — Terraform `helm_release` orchestration
- `1a.6` — eval on k8s + observability + finishing touches

## Toolchain versions (verified on WSL2 / Ubuntu 22.04.5 / kernel 6.6.114)

| Tool           | Version           | Install                                           |
| -------------- | ----------------- | ------------------------------------------------- |
| Docker Desktop | 28.0.4 (server)   | Windows installer + WSL2 integration              |
| kubectl        | v1.32.2           | apt (system)                                      |
| kind           | v0.27.0           | direct binary → `~/.local/bin`                    |
| Helm           | v3.17.0           | direct binary → `~/.local/bin`                    |
| Terraform      | v1.10.5           | direct binary → `~/.local/bin`                    |

systemd is enabled in `/etc/wsl.conf` (`[boot] systemd=true`).
