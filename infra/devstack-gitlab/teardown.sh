#!/usr/bin/env bash
# ============================================================================
# Teardown / cleanup for the devstack+gitlab test harness (setup.sh).
#
#   bash infra/devstack-gitlab/teardown.sh            # stop services, keep VMs
#   STOP_VMS=1 bash infra/devstack-gitlab/teardown.sh # also power off the VMs
#   FULL=1     bash infra/devstack-gitlab/teardown.sh # also remove unit files
#                                                       and the secgroup rule
#
# Never touches: the repo, evaluation/journal, DevStack itself, or the
# host br-ex/NAT (those are shared infra; see project memory
# project_devstack_wsl_reboot_recovery).
# ============================================================================
set -uo pipefail

VM_FIP="${VM_FIP:-172.24.4.71}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/sdlcma_1b}"
SSH_USER="${SSH_USER:-ubuntu}"
SECGROUP="${SECGROUP:-sdlcma-1b-sg}"
DEVSTACK_DIR="${DEVSTACK_DIR:-/opt/stack/devstack}"
VM="${SSH_USER}@${VM_FIP}"
SSH="ssh -i ${SSH_KEY} -o StrictHostKeyChecking=no -o ConnectTimeout=8"
FULL="${FULL:-0}"; STOP_VMS="${STOP_VMS:-0}"

echo "=== stop VM services + kill stale workers ==="
$SSH "$VM" "bash -s" <<REMOTE 2>/dev/null || echo "(VM unreachable — skipping VM-side)"
pkill -f 'bf_worker/bf_worker.py' 2>/dev/null || true
sudo systemctl stop sdlcma-orchestrator sdlcma-gateway gitlab-fwd 2>/dev/null || true
if [ "${FULL}" = 1 ]; then
  sudo systemctl disable sdlcma-orchestrator sdlcma-gateway gitlab-fwd 2>/dev/null || true
  sudo rm -f /etc/systemd/system/sdlcma-gateway.service \
             /etc/systemd/system/sdlcma-orchestrator.service \
             /etc/systemd/system/gitlab-fwd.service
  sudo rm -rf /etc/systemd/system/sdlcma-orchestrator.service.d
  sudo systemctl daemon-reload
fi
redis-cli -n 15 DEL gateway:stream >/dev/null 2>&1 || true
echo "VM services: orch=\$(systemctl is-active sdlcma-orchestrator) gw=\$(systemctl is-active sdlcma-gateway) fwd=\$(systemctl is-active gitlab-fwd)"
REMOTE

echo "=== stop host webhook relay ==="
sudo systemctl stop sdlcma-relay8000.service 2>/dev/null || true
if [ "${FULL}" = 1 ]; then
  sudo systemctl disable sdlcma-relay8000.service 2>/dev/null || true
  sudo rm -f /etc/systemd/system/sdlcma-relay8000.service
  sudo systemctl daemon-reload
fi

if [ "${STOP_VMS}" = 1 ]; then
  echo "=== power off VMs (frees ~the VM RAM) ==="
  ( cd "${DEVSTACK_DIR}" && source openrc admin admin >/dev/null 2>&1
    openstack server stop sdlcma-1b-control-0 sdlcma-1b-worker-0 2>/dev/null || true )
fi

if [ "${FULL}" = 1 ]; then
  echo "=== remove secgroup tcp/8000 rule ==="
  ( cd "${DEVSTACK_DIR}" && source openrc admin admin >/dev/null 2>&1
    SG=$(openstack security group list -f value -c ID -c Name 2>/dev/null | awk -v s="${SECGROUP}" '$2==s{print $1}')
    RID=$(openstack security group rule list "$SG" -f value -c ID -c "Port Range" 2>/dev/null | awk '$2=="8000:8000"{print $1}')
    [ -n "${RID:-}" ] && openstack security group rule delete "$RID" 2>/dev/null || true )
fi

echo "=== done. (DevStack, br-ex/NAT, repo, journal left intact) ==="
