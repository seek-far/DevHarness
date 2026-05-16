#!/usr/bin/env bash
# ============================================================================
# SDLCMA GitLab-mode test harness — "Option 2": run gateway+orchestrator+worker
# INSIDE a DevStack OpenStack VM, with GitLab as a docker-compose container on
# the Windows host. Idempotent: safe to re-run. Counterpart: teardown.sh.
#
# This script encodes every environment change discovered while bringing the
# devstack+gitlab path up, so the setup is reproducible instead of tribal
# knowledge. The actual product fix (orchestrator/parser.py auto/bf branch
# recognition + tests/test_parse_branch.py) lives in the repo, NOT here.
#
# Run from the WSL host (where DevStack runs and Docker Desktop is integrated).
#   bash infra/devstack-gitlab/setup.sh
#
# Prereqomgs the script assumes already exist (one-time, not re-created here):
#   - DevStack installed at /opt/stack/devstack with the sdlcma-1b VMs
#     provisioned via infra/terraform/iaas-openstack (image ubuntu-22.04).
#   - SSH keypair ~/.ssh/sdlcma_1b matching the injected public key.
#   - GitLab docker-compose up on Windows, published :8080->80 / :2222->22,
#     external_url http://gitlab.local.
#   - The WSL-reboot network recovery (br-ex + egress NAT) — this script
#     re-applies it too, so a fresh boot is fine.
# ============================================================================
set -euo pipefail

# ---- Tunables (defaults are the values discovered on this machine) ----------
VM_FIP="${VM_FIP:-172.24.4.71}"                 # control VM floating IP
WIN_GW="${WIN_GW:-192.168.128.1}"               # WSL->Windows gateway (GitLab reachable here)
SSH_KEY="${SSH_KEY:-$HOME/.ssh/sdlcma_1b}"
SSH_USER="${SSH_USER:-ubuntu}"
REPO_DIR="${REPO_DIR:-/mnt/d/my_git/sdlcma/v08}"
OS_CLOUD="${OS_CLOUD:-devstack-admin}"
SECGROUP="${SECGROUP:-sdlcma-1b-sg}"
PUBLIC_SUBNET_GW="${PUBLIC_SUBNET_GW:-172.24.4.1}"   # DevStack public-subnet gateway
PUBLIC_CIDR="${PUBLIC_CIDR:-172.24.4.0/24}"
DEVSTACK_DIR="${DEVSTACK_DIR:-/opt/stack/devstack}"
VM="${SSH_USER}@${VM_FIP}"
SSH="ssh -i ${SSH_KEY} -o StrictHostKeyChecking=no -o ConnectTimeout=8"

say() { printf '\n=== %s ===\n' "$*"; }

# ---- Phase 1: host — DevStack network recovery (idempotent) -----------------
say "1. DevStack services + br-ex + egress NAT (host)"
sudo systemctl start 'devstack@*.service' 2>/dev/null || true
sudo ip link set br-ex up
ip addr show br-ex | grep -q "${PUBLIC_SUBNET_GW}/" \
  || sudo ip addr add "${PUBLIC_SUBNET_GW}/24" dev br-ex
sudo iptables -t nat -C POSTROUTING -s "${PUBLIC_CIDR}" ! -d "${PUBLIC_CIDR}" -j MASQUERADE 2>/dev/null \
  || sudo iptables -t nat -A POSTROUTING -s "${PUBLIC_CIDR}" ! -d "${PUBLIC_CIDR}" -j MASQUERADE
sudo iptables -C FORWARD -i br-ex -o eth0 -j ACCEPT 2>/dev/null \
  || sudo iptables -I FORWARD 1 -i br-ex -o eth0 -j ACCEPT
sudo iptables -C FORWARD -i eth0 -o br-ex -m state --state RELATED,ESTABLISHED -j ACCEPT 2>/dev/null \
  || sudo iptables -I FORWARD 1 -i eth0 -o br-ex -m state --state RELATED,ESTABLISHED -j ACCEPT

# ---- Phase 2: host — start VMs + open secgroup port 8000 --------------------
say "2. Start OpenStack VMs + secgroup tcp/8000"
( cd "${DEVSTACK_DIR}" && source openrc admin admin >/dev/null 2>&1
  for n in sdlcma-1b-control-0 sdlcma-1b-worker-0; do
    st=$(openstack server show "$n" -f value -c status 2>/dev/null || echo MISSING)
    [ "$st" = ACTIVE ] || openstack server start "$n" 2>/dev/null || true
  done
  # secgroup: tcp/8000 ingress for the gateway (terraform module does NOT open it)
  SG=$(openstack security group list -f value -c ID -c Name 2>/dev/null | awk -v s="${SECGROUP}" '$2==s{print $1}')
  openstack security group rule list "$SG" -f value -c "Port Range" 2>/dev/null | grep -q '8000:8000' \
    || openstack security group rule create --proto tcp --dst-port 8000 --ingress --remote-ip 0.0.0.0/0 "$SG" >/dev/null
)
say "2b. Wait for SSH"
for i in $(seq 1 24); do $SSH "$VM" true 2>/dev/null && break; sleep 5; done

# ---- Phase 3: ship repo to the VM ------------------------------------------
say "3. rsync repo -> VM:~/sdlcma (excludes heavy/irrelevant)"
rsync -az --delete \
  --exclude='.git' --exclude='.venv*' \
  --exclude='evaluation/runs/*' --exclude='evaluation/journal/*' \
  --exclude='infra/terraform/*/.terraform' --exclude='infra' \
  --exclude='__pycache__' --exclude='*.pyc' \
  -e "$SSH" "${REPO_DIR}/" "${VM}:~/sdlcma/"

# ---- Phase 4: provision the VM (the gotchas, all idempotent) ---------------
say "4. VM provisioning (python/venv/redis/git identity/systemd units)"
$SSH "$VM" "WIN_GW='${WIN_GW}' bash -s" <<'REMOTE'
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
sudo apt-get update -qq
# python-is-python3: apply_change_and_test shells out to bare `python -m venv`
sudo apt-get install -y -qq python3-venv python3-pip socat redis-server python-is-python3 >/dev/null
sudo systemctl enable --now redis-server >/dev/null 2>&1 || true
# git identity MUST be --system (HOME-independent): worker runs git via
# subprocess(env=os.environ.copy()) under systemd which has no HOME.
sudo git config --system user.email "ci-agent@sdlcma.local"
sudo git config --system user.name  "SDLCMA CI Agent"
cd ~/sdlcma
[ -x .venv-linux/bin/python ] || python3 -m venv .venv-linux
. .venv-linux/bin/activate
python -m pip install -q --upgrade pip
pip install -q -r requirements.txt

# gitlab-fwd: VM localhost:8080 -> GitLab on Windows. This is what makes the
# hardcoded local_multi_process rewrite (gitlab.local -> localhost:8080) AND
# GITLAB_API=http://localhost:8080 resolve to the real GitLab with ZERO code change.
sudo tee /etc/systemd/system/gitlab-fwd.service >/dev/null <<UNIT
[Unit]
Description=VM localhost:8080 -> GitLab on Windows host
After=network.target
[Service]
ExecStart=/usr/bin/socat TCP-LISTEN:8080,fork,reuseaddr TCP:${WIN_GW}:8080
Restart=always
RestartSec=2
[Install]
WantedBy=multi-user.target
UNIT

V=/home/ubuntu/sdlcma; PY=$V/.venv-linux/bin
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

sudo systemctl daemon-reload
sudo systemctl enable --now gitlab-fwd.service sdlcma-gateway.service sdlcma-orchestrator.service
sleep 4
for s in gitlab-fwd sdlcma-gateway sdlcma-orchestrator redis-server; do
  printf '  %-20s %s\n' "$s" "$(systemctl is-active "$s")"
done
curl -sS -m6 -o /dev/null -w "  VM gateway /healthz HTTP %{http_code}\n" http://localhost:8000/healthz || true
REMOTE

# ---- Phase 5: host — webhook ingress relay ---------------------------------
say "5. Host webhook relay :8000 -> VM:8000 (systemd)"
sudo tee /etc/systemd/system/sdlcma-relay8000.service >/dev/null <<UNIT
[Unit]
Description=SDLCMA webhook relay: WSL host :8000 -> DevStack VM ${VM_FIP}:8000
After=network.target
[Service]
ExecStart=/usr/bin/socat TCP-LISTEN:8000,fork,reuseaddr TCP:${VM_FIP}:8000
Restart=always
RestartSec=2
[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable --now sdlcma-relay8000.service
sleep 2
curl -sS -m6 -o /dev/null -w "host relay -> VM gateway /healthz HTTP %{http_code}\n" \
  http://127.0.0.1:8000/healthz || true

cat <<DONE

=== SETUP COMPLETE ===
Manual step that cannot be scripted (GitLab UI):
  In the target project (e.g. lishu2016/order_be) → Settings → Webhooks:
    URL:     http://host.docker.internal:8000/webhook
    Trigger: Pipeline events  (+ Job events)
Then push a failing commit / re-run the pipeline. Watch:
  ssh -i ${SSH_KEY} ${VM} 'sudo journalctl -u sdlcma-orchestrator -f'
Expected terminal state: journal record outcome="fixed", an MR opened from
auto/bf/<bug_id>-<sha8> into main (the agent never commits to main).
Teardown: bash infra/devstack-gitlab/teardown.sh
DONE
