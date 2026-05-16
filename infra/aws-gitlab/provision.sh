#!/usr/bin/env bash
# ============================================================================
# Provision the SDLCMA stack on a GENERIC public Ubuntu host for GitLab mode
# against gitlab.com (SaaS). Same provisioning as infra/devstack-gitlab but
# with ALL the WSL/OpenStack/relay/gitlab-fwd plumbing removed — gitlab.com is
# public, so the worker clones over HTTPS and the webhook reaches the host
# directly (here via a free cloudflared tunnel, no inbound port opened).
#
# Targets ANY reachable Ubuntu host over SSH:
#   * Phase 0.5: your existing weak public-IP box ($0 rehearsal)
#   * Phase 2/3: an AWS EC2 instance (identical script → near-zero AWS cost)
#
#   HOST=ubuntu@1.2.3.4 SSH_KEY=~/.ssh/id_xxx bash infra/aws-gitlab/provision.sh
#
# Idempotent. Counterpart: teardown.sh. Tests: eval-on-host.sh, gitlab-smoke.sh.
# ============================================================================
set -euo pipefail

HOST="${HOST:?set HOST=user@ip}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_rsa}"
REPO_DIR="${REPO_DIR:-/mnt/d/my_git/sdlcma/v08}"
SSH="ssh -i ${SSH_KEY} -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10"

say() { printf '\n=== %s ===\n' "$*"; }

say "1. rsync repo -> ${HOST}:~/sdlcma"
rsync -az --delete \
  --exclude='.git' --exclude='.venv*' \
  --exclude='evaluation/runs/*' --exclude='evaluation/journal/*' \
  --exclude='infra/terraform/*/.terraform' --exclude='infra/terraform' \
  --exclude='__pycache__' --exclude='*.pyc' \
  -e "$SSH" "${REPO_DIR}/" "${HOST}:~/sdlcma/"

say "2. provision host (python/redis/git identity/venv) + systemd + cloudflared"
$SSH "$HOST" 'bash -s' <<'REMOTE'
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
sudo apt-get update -qq
# python-is-python3: apply_change_and_test shells out to bare `python -m venv`
sudo apt-get install -y -qq python3-venv python3-pip redis-server python-is-python3 curl >/dev/null
sudo systemctl enable --now redis-server >/dev/null 2>&1 || true
# git identity via --system (HOME-independent; systemd services have no HOME)
sudo git config --system user.email "ci-agent@sdlcma.local"
sudo git config --system user.name  "SDLCMA CI Agent"

cd ~/sdlcma
# Two-step config: ENV=gitlab_saas. Seed real env files from .example on first
# run; the token MUST be filled in by hand (kept out of git).
printf 'ENV=gitlab_saas\n' > settings/.env
for c in worker orchestrator; do
  [ -f settings/${c}_gitlab_saas.env ] || cp settings/${c}_gitlab_saas.env.example settings/${c}_gitlab_saas.env
done
grep -q 'your_gitlab_com_access_token' settings/worker_gitlab_saas.env 2>/dev/null \
  && echo "  !! settings/worker_gitlab_saas.env still has placeholder token — fill it before testing"

[ -x .venv-linux/bin/python ] || python3 -m venv .venv-linux
. .venv-linux/bin/activate
python -m pip install -q --upgrade pip
pip install -q -r requirements.txt

V="$HOME/sdlcma"; PY="$V/.venv-linux/bin"
sudo tee /etc/systemd/system/sdlcma-gateway.service >/dev/null <<UNIT
[Unit]
Description=SDLCMA gateway
After=network.target redis-server.service
[Service]
WorkingDirectory=$V
ExecStart=$PY/uvicorn gateway.gateway:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=3
[Install]
WantedBy=multi-user.target
UNIT
sudo tee /etc/systemd/system/sdlcma-orchestrator.service >/dev/null <<UNIT
[Unit]
Description=SDLCMA orchestrator
After=network.target redis-server.service sdlcma-gateway.service
[Service]
WorkingDirectory=$V
Environment=HOME=/root
ExecStart=$PY/python -m orchestrator.orchestrator
Restart=always
RestartSec=3
[Install]
WantedBy=multi-user.target
UNIT

# cloudflared free quick-tunnel → public HTTPS URL for the webhook, ZERO
# inbound ports opened (works behind NAT / strict firewalls too).
if ! command -v cloudflared >/dev/null 2>&1; then
  curl -fsSL -o /tmp/cf.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
  sudo dpkg -i /tmp/cf.deb >/dev/null 2>&1 || sudo apt-get -f install -y -qq
fi
sudo tee /etc/systemd/system/sdlcma-cf.service >/dev/null <<UNIT
[Unit]
Description=cloudflared quick tunnel -> SDLCMA gateway
After=network.target sdlcma-gateway.service
[Service]
ExecStart=/usr/bin/cloudflared tunnel --no-autoupdate --url http://localhost:8000
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now sdlcma-gateway.service sdlcma-orchestrator.service sdlcma-cf.service
sleep 6
for s in redis-server sdlcma-gateway sdlcma-orchestrator sdlcma-cf; do
  printf '  %-20s %s\n' "$s" "$(systemctl is-active "$s")"
done
curl -sS -m6 -o /dev/null -w "  gateway /healthz HTTP %{http_code}\n" http://localhost:8000/healthz || true
echo "  cloudflared URL:"
sudo journalctl -u sdlcma-cf --no-pager 2>/dev/null | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | tail -1 | sed 's/^/    /'
REMOTE

cat <<DONE

=== PROVISION COMPLETE ===
Before testing:
  1. Put a short-lived gitlab.com Project/Group access token (scopes:
     api + write_repository) into settings/worker_gitlab_saas.env on the host:
       ssh ... 'sed -i s/your_gitlab_com_access_token/<TOKEN>/ ~/sdlcma/settings/worker_gitlab_saas.env'
       ssh ... 'sudo systemctl restart sdlcma-orchestrator sdlcma-gateway'
  2. In the gitlab.com test project → Settings → Webhooks:
       URL = <the https://...trycloudflare.com URL printed above>/webhook
       Trigger = Pipeline events (+ Job events)
Then: bash infra/aws-gitlab/gitlab-smoke.sh   (or trigger a failing pipeline)
Eval (no GitLab needed): bash infra/aws-gitlab/eval-on-host.sh
Teardown: bash infra/aws-gitlab/teardown.sh
DONE
