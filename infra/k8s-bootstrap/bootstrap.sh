#!/usr/bin/env bash
# 1b.3 — kubeadm cluster bootstrap on the TF-provisioned OpenStack VMs.
#
# Runs from the host. Reads the IaaS root's Terraform outputs for node IPs,
# waits for the cloud-init prereqs, then drives kubeadm init/join + CNI and
# pulls back a kubeconfig pointed at the control node's floating IP.
#
# Idempotent: re-running skips an already-inited control / already-joined
# worker, so it composes with `terraform apply` re-runs.
#
# Usage: bootstrap.sh [TF_ROOT] [SSH_KEY]
set -euo pipefail

TF_ROOT="${1:-$(cd "$(dirname "$0")/../terraform/iaas-openstack" && pwd)}"
SSH_KEY="${2:-$HOME/.ssh/sdlcma_1b}"
POD_CIDR="10.244.0.0/16"
KUBECONFIG_OUT="$HOME/.kube/sdlcma-1b.config"
FLANNEL_URL="https://github.com/flannel-io/flannel/releases/latest/download/kube-flannel.yml"

SSH_OPTS=(-i "$SSH_KEY" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null
          -o ConnectTimeout=10 -o BatchMode=yes)

log() { printf '\n=== %s ===\n' "$*"; }

# --- read terraform outputs --------------------------------------------------
log "reading terraform outputs from $TF_ROOT"
OUT_JSON="$(terraform -chdir="$TF_ROOT" output -json)"
SSH_USER="$(jq -r '.ssh_user.value' <<<"$OUT_JSON")"
CONTROL_FIP="$(jq -r '.control_ips.value[0]' <<<"$OUT_JSON")"
mapfile -t WORKER_IPS < <(jq -r '.worker_ips.value[]' <<<"$OUT_JSON")
echo "control_fip=$CONTROL_FIP ssh_user=$SSH_USER workers=${WORKER_IPS[*]}"

ssh_c() { ssh "${SSH_OPTS[@]}" "$SSH_USER@$CONTROL_FIP" "$@"; }

# --- wait for ssh + cloud-init prereqs on control ----------------------------
log "waiting for SSH on control ($CONTROL_FIP)"
for i in $(seq 1 30); do
  ssh_c true 2>/dev/null && break
  [ "$i" -eq 30 ] && { echo "control SSH never came up"; exit 1; }
  sleep 5
done
log "waiting for cloud-init prereqs on control"
ssh_c 'cloud-init status --wait >/dev/null 2>&1; until [ -f /var/lib/cloud-init-k8s-prereqs.done ]; do sleep 3; done; echo prereqs-ok'

# Workers reach the API on the control node's *private* IP (same subnet).
CONTROL_PRIV="$(ssh_c "ip route get 1.1.1.1 | awk '{print \$7; exit}'")"
echo "control private ip = $CONTROL_PRIV"

# --- kubeadm init on control (idempotent) ------------------------------------
if ssh_c 'test -f /etc/kubernetes/admin.conf'; then
  log "control already initialised — skipping kubeadm init"
else
  log "kubeadm init on control"
  ssh_c "sudo kubeadm init \
    --pod-network-cidr=$POD_CIDR \
    --apiserver-advertise-address=$CONTROL_PRIV \
    --apiserver-cert-extra-sans=$CONTROL_FIP"
fi

log "configuring kubectl for $SSH_USER on control"
ssh_c 'mkdir -p $HOME/.kube && sudo cp -f /etc/kubernetes/admin.conf $HOME/.kube/config && sudo chown $(id -u):$(id -g) $HOME/.kube/config'

# --- CNI (flannel; its default net matches POD_CIDR) -------------------------
if ssh_c 'kubectl get ns kube-flannel >/dev/null 2>&1'; then
  log "flannel already installed — skipping"
else
  log "installing flannel CNI"
  ssh_c "kubectl apply -f $FLANNEL_URL"
fi

# --- join workers (idempotent) -----------------------------------------------
JOIN_CMD="$(ssh_c 'sudo kubeadm token create --print-join-command')"
for w in "${WORKER_IPS[@]}"; do
  log "joining worker $w"
  # Hop through control (workers have no floating IP). ProxyJump does NOT
  # inherit -i for the jump hop, so use an explicit keyed ProxyCommand.
  PROXY="ssh -i $SSH_KEY -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -W %h:%p $SSH_USER@$CONTROL_FIP"
  WSSH=(ssh "${SSH_OPTS[@]}" -o "ProxyCommand=$PROXY" "$SSH_USER@$w")
  for i in $(seq 1 30); do
    "${WSSH[@]}" true 2>/dev/null && break
    [ "$i" -eq 30 ] && { echo "worker $w SSH never came up"; exit 1; }
    sleep 5
  done
  "${WSSH[@]}" 'cloud-init status --wait >/dev/null 2>&1; until [ -f /var/lib/cloud-init-k8s-prereqs.done ]; do sleep 3; done'
  if "${WSSH[@]}" 'test -f /etc/kubernetes/kubelet.conf'; then
    echo "worker $w already joined — skipping"
  else
    "${WSSH[@]}" "sudo $JOIN_CMD"
  fi
done

# --- pull kubeconfig pointed at the floating IP ------------------------------
log "fetching kubeconfig -> $KUBECONFIG_OUT"
mkdir -p "$(dirname "$KUBECONFIG_OUT")"
ssh_c 'sudo cat /etc/kubernetes/admin.conf' \
  | sed -E "s#server: https://[0-9.]+:6443#server: https://$CONTROL_FIP:6443#" \
  > "$KUBECONFIG_OUT"
chmod 600 "$KUBECONFIG_OUT"

# --- verify from the host ----------------------------------------------------
log "waiting for all nodes Ready (via $CONTROL_FIP)"
EXPECT=$(( 1 + ${#WORKER_IPS[@]} ))
for i in $(seq 1 40); do
  READY="$(KUBECONFIG="$KUBECONFIG_OUT" kubectl get nodes --no-headers 2>/dev/null \
            | awk '$2=="Ready"' | wc -l)"
  echo "ready $READY/$EXPECT"
  [ "$READY" -ge "$EXPECT" ] && break
  [ "$i" -eq 40 ] && { echo "not all nodes Ready in time"; KUBECONFIG="$KUBECONFIG_OUT" kubectl get nodes; exit 1; }
  sleep 6
done
KUBECONFIG="$KUBECONFIG_OUT" kubectl get nodes -o wide
echo "BOOTSTRAP_OK kubeconfig=$KUBECONFIG_OUT"
