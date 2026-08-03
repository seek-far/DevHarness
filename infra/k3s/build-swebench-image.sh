#!/usr/bin/env bash
# ============================================================================
# infra/k3s/build-swebench-image.sh — build the ver99 worker image (plan W4).
#
# Produces `dh-bf-worker-swebench:<sdlcma-short-sha>` = the normal bf-worker
# image plus the three things ver99 needs in a pod and the base image does not
# have: the docker CLI, the mini-swe-agent package, and configs/.
#
# Usage:
#   bash infra/k3s/build-swebench-image.sh
#   PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple bash ...   # CN host
#   DOCKER_CLI_URL=https://mirrors.aliyun.com/docker-ce/linux/static/stable/x86_64/docker-27.5.1.tgz bash ...
#   SKIP_BASE=1 bash ...        # base image already built
#
# Why an immutable tag and not :latest — the chart pulls with
# imagePullPolicy: IfNotPresent and images are side-loaded via
# `k3s ctr images import` with no registry to re-pull from, so two nodes can
# hold different content under the same moving tag and nothing will say so.
# The tag names the BUILD; the ingredients go in OCI labels.
#
# Exit codes: 0 ok / 4 pre-flight
# ============================================================================
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_DIR"

say()   { printf '[build-swebench] %s\n' "$*"; }
abort() { echo "ABORT: $1" >&2; exit "${2:-4}"; }

command -v docker >/dev/null || abort "docker not found"
command -v git    >/dev/null || abort "git not found"

PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null || abort "$PY not found (set PYTHON=)"

# ── 1: locate the INSTALLED mini-swe-agent ─────────────────────────────────
# Same rule as tests/test_vendor_mini_parity.py: vendor from the installed
# package, never from some other checkout lying around. Other clones exist at
# other versions and copying from one silently diverges.
say "1: locating the installed mini-swe-agent"
# MSWEA_SILENT_STARTUP: minisweagent prints a banner + "Loading global config"
# to STDOUT on import, which command substitution would capture as part of the
# path. Same setdefault the test modules use.
MINI_SRC=$(MSWEA_SILENT_STARTUP=1 "$PY" - <<'PY' 2>/dev/null
import pathlib
try:
    import minisweagent
except Exception:
    raise SystemExit(1)
print(pathlib.Path(minisweagent.__file__).resolve().parents[2])
PY
) || abort "mini-swe-agent is not importable — install the fork first (pip install -e /path/to/mini-swe-agent)"
[ -d "$MINI_SRC" ] || abort "resolved mini source '$MINI_SRC' is not a directory"

MINI_VERSION=$(MSWEA_SILENT_STARTUP=1 "$PY" -c 'import minisweagent;print(minisweagent.__version__)' 2>/dev/null || echo "")

# ── provenance ─────────────────────────────────────────────────────────────
# Mandatory, not best-effort: an image labelled with an EMPTY mini-commit is
# worse than one with no label, because it looks authoritative while saying
# nothing — and "which fork is on this node" is the whole reason the label
# exists.
#
# But it cannot come from git here. A DEPLOY host has neither history: both
# ~/sdlcma and ~/mini-swe-agent are rsync'd trees with no .git (measured on
# ls4900). So the source machine — which does have git — stamps
# .sdlcma-provenance at deploy time, and this script reads it. Precedence:
# local git (dev box) > stamp file (deploy host) > explicit env > refuse.
#
# Produce the stamp on the machine you rsync FROM:
#   { echo "SDLCMA_COMMIT=$(git rev-parse --short HEAD)$(git diff --quiet || echo -dirty)"
#     echo "MINI_COMMIT=<mini short sha>"
#     echo "MINI_VERSION=<x.y.z>"; } > .sdlcma-provenance
STAMP=".sdlcma-provenance"
[ -f "$STAMP" ] && . "$STAMP"

if git -C "$MINI_SRC" rev-parse --git-dir >/dev/null 2>&1; then
  MINI_COMMIT=$(git -C "$MINI_SRC" rev-parse --short HEAD)
  git -C "$MINI_SRC" diff --quiet 2>/dev/null || MINI_COMMIT="${MINI_COMMIT}-dirty"
fi
[ -n "${MINI_COMMIT:-}" ] || abort "cannot determine the mini-swe-agent commit.
   $MINI_SRC is not a git checkout and no MINI_COMMIT was supplied.
   Either install the fork as an editable git clone, or stamp
   $STAMP from the machine you deployed from (see the comment above),
   or pass MINI_COMMIT=<sha> explicitly. Refusing to build an image whose
   provenance label would be a lie."
say "   mini: $MINI_SRC  ($MINI_VERSION @ $MINI_COMMIT)"

PINNED=$(grep -oE '\b[0-9a-f]{8,40}\b' bf_worker/agents/vendor/mini/UPSTREAM.md | head -1 || true)
if [ -n "$PINNED" ] && [ "${MINI_COMMIT%-dirty}" != "${PINNED:0:${#MINI_COMMIT}}" ]; then
  say "   WARNING: installed mini ($MINI_COMMIT) differs from the vendored pin"
  say "            in UPSTREAM.md ($PINNED). tests/test_vendor_mini_parity.py"
  say "            will tell you whether that is a real divergence."
fi

# ── 2: wheel ───────────────────────────────────────────────────────────────
# wheels/ specifically: build/ and dist/ are excluded by .dockerignore, so a
# wheel placed there would be invisible to the COPY and the build would fail
# with a confusing "no such file".
say "2: building the mini wheel into wheels/"
rm -rf wheels && mkdir -p wheels
WHEEL_LOG=$(mktemp)
# pip is not a given. The deploy host's venvs are built by `uv venv`, which
# does NOT install pip, and its system python3 has none either (measured on
# ls4900) — so a hardcoded `pip wheel` fails on exactly the machine this
# script exists for. Try pip, then uv, then say so plainly.
if "$PY" -m pip --version >/dev/null 2>&1; then
  say "   via pip"
  "$PY" -m pip wheel --no-deps -w wheels "$MINI_SRC" >"$WHEEL_LOG" 2>&1
  WHEEL_RC=$?
elif command -v uv >/dev/null 2>&1; then
  say "   via uv build (no pip in this environment)"
  # uv reads the index from UV_DEFAULT_INDEX (new) / UV_INDEX_URL (older); mirror
  # PIP_INDEX_URL into both so one CN setting covers every path in this script.
  UV_DEFAULT_INDEX="${UV_DEFAULT_INDEX:-${PIP_INDEX_URL:-}}" \
  UV_INDEX_URL="${UV_INDEX_URL:-${PIP_INDEX_URL:-}}" \
    uv build --wheel --out-dir wheels "$MINI_SRC" >"$WHEEL_LOG" 2>&1
  WHEEL_RC=$?
else
  rm -f "$WHEEL_LOG"
  abort "neither pip nor uv is available to build the wheel.
   Install one, or build mini_swe_agent-*.whl yourself into ./wheels/."
fi
if [ "$WHEEL_RC" -ne 0 ] || ! ls wheels/mini_swe_agent-*.whl >/dev/null 2>&1; then
  # Never swallow this. The first run of this script on ls4900 died here and
  # the only thing on screen was "pip wheel failed" — the actual cause ("No
  # module named pip") was in the discarded output.
  echo "--- wheel build output (tail) ---" >&2
  tail -25 "$WHEEL_LOG" >&2
  rm -f "$WHEEL_LOG"
  abort "wheel build failed for $MINI_SRC"
fi
rm -f "$WHEEL_LOG"
say "   $(ls wheels/mini_swe_agent-*.whl)"

# ── 3: tags ────────────────────────────────────────────────────────────────
# Same precedence as the mini commit above, and for the same reason: a deploy
# host has no git history to ask.
if git rev-parse --git-dir >/dev/null 2>&1; then
  SHA=$(git rev-parse --short HEAD)
  git diff --quiet || SHA="${SHA}-dirty"
else
  SHA="${SDLCMA_COMMIT:-}"
fi
[ -n "$SHA" ] || abort "cannot determine the sdlcma commit for the image tag.
   $REPO_DIR is not a git checkout and no SDLCMA_COMMIT was supplied.
   Stamp $STAMP at deploy time, or pass SDLCMA_COMMIT=<sha>.
   A tag that does not identify the build defeats the point of having one."
TAG="dh-bf-worker-swebench:${SHA}"

# ── 4: build ───────────────────────────────────────────────────────────────
if [ "${SKIP_BASE:-0}" != "1" ]; then
  say "4a: base image dh-bf-worker:latest"
  docker build -f Dockerfile.bf-worker -t dh-bf-worker:latest . \
    || abort "base image build failed"
else
  docker image inspect dh-bf-worker:latest >/dev/null 2>&1 \
    || abort "SKIP_BASE=1 but dh-bf-worker:latest does not exist"
fi

say "4b: $TAG"
docker build -f Dockerfile.bf-worker-swebench -t "$TAG" \
  --build-arg "PIP_INDEX_URL=${PIP_INDEX_URL:-}" \
  ${DOCKER_CLI_URL:+--build-arg "DOCKER_CLI_URL=$DOCKER_CLI_URL"} \
  --build-arg "SDLCMA_COMMIT=${SHA}" \
  --build-arg "MINI_COMMIT=${MINI_COMMIT}" \
  --build-arg "MINI_VERSION=${MINI_VERSION}" \
  . || abort "swebench image build failed"

# A convenience alias for poking at it by hand. NEVER referenced by an overlay
# — see the header.
docker tag "$TAG" dh-bf-worker-swebench:latest

echo
say "built $TAG"
say "  mini-commit=$MINI_COMMIT  mini-version=$MINI_VERSION"
echo
echo "  Next:"
echo "    1. put this in infra/helm/sdlcma/values-k3s-ver99.yaml:"
echo "         configMaps: {orchestrator: {WORKER_IMAGE: $TAG}}"
echo "    2. import it on every node that may run a worker:"
echo "         bash infra/k3s/load-image.sh --node all $TAG"
