#!/usr/bin/env bash
# ============================================================================
# infra/k3s/load-image.sh — publish locally-built images into k3s containerd.
#
# k3s has no `kind load`. Each node runs its OWN containerd, and that store is
# separate from the host docker daemon's — so an image that `docker images`
# shows is invisible to kubelet until it is imported. The chart uses
# imagePullPolicy=IfNotPresent with :latest tags and there is no registry, so
# a missing import surfaces as ImagePullBackOff.
#
#   ⚠️ Two ways to lose an afternoon here:
#
#   1. `-n k8s.io` is NOT optional. containerd namespaces the image store;
#      an import into the default namespace leaves `ctr images ls` showing
#      the image while kubelet still can't find it.
#   2. Re-importing the same :latest tag does NOT restart running pods.
#      kubelet already resolved the tag. setup.sh rollout-restarts after
#      importing; if you call this script directly, do that yourself.
#
# Usage:
#   bash infra/k3s/load-image.sh                  # local node: all 4 + redis
#   bash infra/k3s/load-image.sh --node minus     # remote node: dh-bf-worker ONLY
#   bash infra/k3s/load-image.sh --node all
#   bash infra/k3s/load-image.sh --node minus IMG [IMG…]   # explicit override
#
# The per-target defaults differ on purpose: every service Deployment is
# pinned to the server node (docs/k3s.md §9.1), so a remote node only ever
# runs bf-worker Jobs. Shipping the other four across an ocean would move
# ~1 GB for images nothing there can schedule.
#
# Env:
#   K3S_MINUS_SSH   ssh command reaching the remote node, e.g.
#                   "ssh -i ~/.ssh/id_ed25519 user@minus-wsl". When unset (the
#                   default) the remote branch PRINTS the two commands to run
#                   there instead of running them. That is a first-class path,
#                   not a degraded one: sudo is password-gated on both nodes
#                   (verified 2026-08-02), so an unattended pipe would hang on
#                   a password prompt with no output — the exact failure mode
#                   infra/k3s/setup.sh's need_sudo() exists to avoid.
#
# Exit codes: 0 ok / 4 pre-flight / 10 needs a human (sudo / remote shell)
# ============================================================================
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_DIR"

NODE="local"
IMAGES=()
while [ $# -gt 0 ]; do
  case "$1" in
    --node) NODE="$2"; shift 2 ;;
    -h|--help) sed -n '2,40p' "$0"; exit 0 ;;
    -*) echo "unknown arg: $1" >&2; exit 4 ;;
    *) IMAGES+=("$1"); shift ;;
  esac
done

# Defaults differ per target, because the two nodes run different things.
#
# LOCAL (the server node) hosts every Deployment, so it needs all four of ours
# plus redis. redis is in the list for the same reason ours are: the node's
# containerd cannot reach docker.io on a CN network, while the host docker
# daemon already has the image via its own mirror.
DEFAULT_LOCAL=(dh-gateway:latest dh-orchestrator:latest dh-bf-worker:latest \
               dh-llm-gateway:latest redis:7-alpine)
# REMOTE nodes only ever run bf-worker Jobs — every service is pinned to the
# server node by the overlay's nodeSelector (docs/k3s.md §9.1). Shipping the
# other four would push ~1 GB across an ocean for images nothing there can
# schedule. Pass image names explicitly to override.
DEFAULT_REMOTE=(dh-bf-worker:latest)

# cloudflared is absent from both on purpose — the k3s overlay disables it
# (GitLab lives on the same host as the cluster).

say()   { printf '[load-image] %s\n' "$*"; }
abort() { echo "ABORT: $1" >&2; exit "${2:-4}"; }

command -v docker >/dev/null || abort "docker not found"

# Resolve the list for one target: explicit argument wins, else the per-target
# default. Validated here rather than up front, since the two targets can have
# different lists in a single `--node all` run.
_resolve() {   # $1 = local|remote  → echoes the image list
  if [ ${#IMAGES[@]} -gt 0 ]; then printf '%s\n' "${IMAGES[@]}"; return; fi
  if [ "$1" = local ]; then printf '%s\n' "${DEFAULT_LOCAL[@]}";
  else printf '%s\n' "${DEFAULT_REMOTE[@]}"; fi
}

_require_present() {
  for img in "$@"; do
    docker image inspect "$img" >/dev/null 2>&1 \
      || abort "image not in the local docker daemon: $img (build it first, or docker pull)"
  done
}

RC=0

# ── local node ─────────────────────────────────────────────────────────────
import_local() {
  local -a imgs
  mapfile -t imgs < <(_resolve local)
  _require_present "${imgs[@]}"
  say "importing ${#imgs[@]} image(s) into the LOCAL node's containerd: ${imgs[*]}"
  if sudo -n true 2>/dev/null; then
    docker save "${imgs[@]}" | sudo k3s ctr -n k8s.io images import -
    return $?
  fi
  cat <<EOF

!! sudo is password-gated here — this script will not prompt (it is routinely
   run non-interactively, where a prompt hangs silently). Run this yourself:

     docker save ${imgs[*]} | sudo k3s ctr -n k8s.io images import -

   Then re-run this script (it is idempotent) or continue with setup.sh.
EOF
  return 10
}

# ── remote node ────────────────────────────────────────────────────────────
import_remote() {
  local node="$1"
  local ssh_var="K3S_$(echo "$node" | tr '[:lower:]-' '[:upper:]_')_SSH"
  local ssh_cmd="${!ssh_var:-}"
  local -a imgs
  mapfile -t imgs < <(_resolve remote)
  _require_present "${imgs[@]}"

  if [ -n "$ssh_cmd" ]; then
    say "streaming ${#imgs[@]} image(s) to $node via \$$ssh_var: ${imgs[*]}"
    # No `docker save` on the far side: the images live HERE. Pipe the tar
    # straight into the remote containerd. gzip because this link may cross
    # an ocean and the images are mostly compressible layers.
    docker save "${imgs[@]}" | gzip \
      | $ssh_cmd "gunzip | sudo k3s ctr -n k8s.io images import -"
    local rc=$?
    [ $rc -eq 0 ] || cat <<EOF

!! remote import failed (rc=$rc). The usual cause is that sudo on $node is
   password-gated too, which a piped command cannot satisfy. Fall back to
   the manual path below (unset $ssh_var to get it printed).
EOF
    return $rc
  fi

  local tar="/tmp/sdlcma-k3s-images.tar.gz"
  say "no \$$ssh_var set — writing $tar (${imgs[*]}) and printing the manual steps"
  docker save "${imgs[@]}" | gzip > "$tar" || return 4
  cat <<EOF

!! Manual step required on node '$node' (no ssh command configured, and sudo
   there is password-gated anyway):

   1. copy the bundle over. The remote node can dial OUT (that is why
      distr-pull works on this topology), so pulling from there is usually
      easier than pushing from here:
        # on $node:
        scp <you>@$(hostname):$tar /tmp/

   2. on $node:
        gunzip -c /tmp/$(basename "$tar") | sudo k3s ctr -n k8s.io images import -
        sudo k3s ctr -n k8s.io images ls | grep dh-      # verify

   Set $ssh_var (e.g. "ssh -i ~/.ssh/key user@$node") to skip the copy step
   next time.
EOF
  return 10
}

case "$NODE" in
  local)  import_local; RC=$? ;;
  all)    import_local; RC=$?; import_remote minus; r2=$?; [ $RC -eq 0 ] && RC=$r2 ;;
  *)      import_remote "$NODE"; RC=$? ;;
esac

if [ $RC -eq 0 ]; then
  say "done. Remember: re-importing :latest does not restart running pods —"
  say "  kubectl -n sdlcma rollout restart deploy/gateway deploy/orchestrator"
fi
exit $RC
