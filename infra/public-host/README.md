# Public-host migration (cloud rehearsal "Phase 0.5")

The Stage-2 containerized stack moved onto a real public-IP host
(`82.165.48.174`), pointing at gitlab.com. Same images, same `gitlab_saas`
auth (HTTPS + `oauth2:<token>`, no SSH) as local Stage-2.

**Ingress reality:** the host *has* a public IP, but its upstream IONOS
provider firewall only permits inbound `22/80/443` (verified: the host
reaches its **own** public IP `:8000` via hairpin → 200, but the external
internet → `:8000` times out; 80/443 belong to the live Caddy/sales-retro
and must not be touched). So inbound webhooks come through a **cloudflared
quick tunnel** (outbound 443 only — not subject to the inbound firewall),
exactly as in local Stage-2. cloudflared is *not* dropped here; the
public-IP-only-helps-if-the-port-is-open assumption was wrong for this box.

```
gitlab.com ──webhook──> https://<rand>.trycloudflare.com/webhook
                          (cloudflared dials OUT; no inbound port needed)
                        → in-stack gateway → orchestrator (gitlab_saas,
                          WORKER_SPAWNER=docker)
                        → per-bug worker container (Docker socket)
                        → clone https://oauth2:<PAT>@gitlab.com/… → fix → MR
```

## Scripts (run from the WSL dev box)

| | |
|---|---|
| `bash infra/public-host/setup.sh`    | install Docker if absent, add a 4 GB swapfile, ship the locally-built `dh-*` images (`docker save\|load`) + the compose files, `compose up -d` |
| `bash infra/public-host/teardown.sh` | `compose down` + `swapoff`/rm swapfile (`FULL=1` also rmi images + rm net) |

Tunables via env: `HOST`, `SSH_KEY` (default `~/.ssh/sales_deploy`),
`SSH_USER` (`root`), `SWAP_GB` (4), `REMOTE_DIR` (`/root/sdlcma-stack`).

## Reboot-OFF semantic (project-wide)

`teardown.sh` is **not a pause**: compose services have **no `restart:`
policy** and teardown removes the containers *and* the swapfile, so a host
reboot brings **nothing** SDLCMA back — only `setup.sh` does. (Docker daemon,
Caddy, and sales-retro are never touched by these scripts.)

## Co-tenant rule (2 GB box)

The host also runs the live `sales-retro` app (`sales_02`, Caddy → 127.0.0.1:
8765). 2 GB RAM, and a worker doing `pip install`+`pytest` is heavy. **Before**
SDLCMA testing, free the RAM:

```
ssh -i ~/.ssh/sales_deploy root@82.165.48.174 'cd /path/sales_02 && bash deploy/teardown.sh'
```

(`sales_02/deploy/{setup,teardown}.sh` use the same reboot-OFF semantic:
teardown = stop + `systemctl disable sales-retro`.) Restore it with
`sales_02/deploy/setup.sh` when done. The 4 GB swap is the documented
mandatory OOM mitigation; keep worker concurrency = 1.

## Webhook

`https://<rand>.trycloudflare.com/webhook` (HTTPS, valid cert — leave SSL
verification ON), Pipeline events. **Not an `ip:port`** — the IONOS firewall
blocks inbound `:8000`, so ingress is the cloudflared tunnel. The URL is
**ephemeral**: it changes every time the `cloudflared` container restarts, so
re-read it (`docker compose … logs cloudflared`, or the tail of `setup.sh`)
and re-set the gitlab.com project webhook on each bring-up. (A Caddy
reverse-proxy TLS site on the already-open `:443` is a possible stable
alternative but would edit the live Caddyfile — deliberately not done.)
