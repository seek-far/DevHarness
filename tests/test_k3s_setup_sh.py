"""Lint-style guards on infra/k3s/*.sh.

These don't exercise the scripts — that needs a live two-node k3s cluster
across two continents. They assert the load-bearing invariants stay in place,
each of which was a real hazard rather than a style preference. The kind
sibling is tests/test_k8s_setup_sh.py; the two harnesses coexist and must not
grow into each other.
"""

from __future__ import annotations

from pathlib import Path

import pytest

K3S = Path(__file__).resolve().parents[1] / "infra" / "k3s"

SETUP = K3S / "setup.sh"
TEARDOWN = K3S / "teardown.sh"
LOAD_IMAGE = K3S / "load-image.sh"
CROSSNODE = K3S / "crossnode-check.sh"
SMOKE = K3S / "gitlab-smoke.sh"

ALL_SCRIPTS = [SETUP, TEARDOWN, LOAD_IMAGE, CROSSNODE, SMOKE]


def _read(p: Path) -> str:
    return p.read_text()


def _code(p: Path) -> str:
    """Script text with comment lines stripped.

    Needed because these scripts document the traps they avoid, so the
    forbidden command appears verbatim in prose ("never run `kubectl config
    use-context`"). A grep over raw text would flag the explanation as the
    offence — and the natural "fix" is deleting the explanation.
    """
    return "\n".join(
        line for line in p.read_text().splitlines()
        if not line.lstrip().startswith("#")
    )


# Lines that only PRINT text (operator hints, next-step suggestions). They
# routinely name commands the script itself never runs, so a check for
# "does this script call kubectl?" has to exclude them or it fires on the
# help text.
_PRINTERS = ("say ", "echo ", "printf ", "cat <<")


def _invocations(p: Path) -> str:
    return "\n".join(
        line for line in _code(p).splitlines()
        if not line.lstrip().startswith(_PRINTERS)
    )


@pytest.mark.parametrize("script", ALL_SCRIPTS, ids=lambda p: p.name)
def test_scripts_exist_and_are_bash(script: Path):
    assert script.exists(), f"{script} missing"
    assert _read(script).startswith("#!/usr/bin/env bash")


@pytest.mark.parametrize("script", ALL_SCRIPTS, ids=lambda p: p.name)
def test_never_switches_the_global_kube_context(script: Path):
    """No script may run `kubectl config use-context`.

    Real incident: installing k3s copied its k3s.yaml over ~/.kube/config,
    deleting the kind context. infra/k8s/teardown.sh guards on that context
    existing, so on that host it silently became a no-op that exits 0 having
    torn down nothing. Two deployment shapes sharing one kubeconfig is how
    that happens. The k3s harness therefore uses a DEDICATED kubeconfig file
    and an explicit --context, and never mutates global kubectl state.
    """
    code = _code(script)
    assert "config use-context" not in code, (
        f"{script.name} switches the global kube context — use "
        "KUBECONFIG=$K3S_KUBECONFIG plus `kubectl --context` instead"
    )
    # Only scripts that actually talk to the API server need to resolve a
    # kubeconfig. load-image.sh drives docker + containerd and never calls
    # kubectl, so requiring it there would be cargo cult.
    if "kubectl" in _invocations(script):
        assert "K3S_KUBECONFIG" in code, (
            f"{script.name} calls kubectl but doesn't pin K3S_KUBECONFIG — it "
            "would inherit whatever ~/.kube/config happens to point at"
        )


def test_setup_taints_the_cross_continent_node():
    """setup.sh must taint the remote node BEFORE installing the chart.

    Worker Jobs carry no resources and no nodeSelector until plan item W4, so
    an untainted cross-continent node is an equally attractive scheduling
    target — a coin flip on every webhook. The taint is the declared default;
    W4 removes it together with the machinery that makes crossing safe.
    """
    content = _read(SETUP)
    assert "taint node" in content and "NoSchedule" in content
    assert "sdlcma.io/cross-continent" in content
    # Ordering: the taint must be applied before `helm upgrade --install`.
    assert content.index("taint node") < content.index("helm --kube-context"), (
        "the taint must be applied BEFORE the chart install, otherwise the "
        "first webhook can land a worker on the remote node"
    )


def test_teardown_keeps_the_taint_by_default():
    """Tearing down a release must not silently re-open cross-ocean scheduling.

    Removing the taint is W4's decision, made together with resources +
    toleration. An opt-in --untaint flag exists for when that is genuinely
    what you want.
    """
    content = _read(TEARDOWN)
    assert "--untaint" in content, "teardown must gate untainting behind a flag"
    assert "retained" in content, (
        "teardown must state that the taint is deliberately kept"
    )


def test_load_image_uses_the_k8s_io_containerd_namespace():
    """`ctr images import` without `-n k8s.io` lands in the wrong containerd
    namespace: `ctr images ls` shows the image while kubelet cannot find it,
    which surfaces as ImagePullBackOff with no obvious cause."""
    content = _read(LOAD_IMAGE)
    assert content.count("-n k8s.io") >= 2, (
        "every `k3s ctr images import` (local and remote) needs -n k8s.io"
    )
    assert "ctr -n k8s.io images import" in content


def test_load_image_has_a_no_credentials_path():
    """sudo is password-gated on BOTH nodes (measured), so a piped
    `ssh host 'sudo …'` hangs on an invisible password prompt when run
    unattended. The printed-command path is a first-class fallback, not a
    degraded one, and must exist."""
    content = _read(LOAD_IMAGE)
    assert "sudo -n true" in content, "must probe for passwordless sudo, not assume it"
    assert "K3S_MINUS_SSH" in content or "_SSH" in content
    assert "Manual step required" in content


def test_setup_checks_redis_isolation_against_the_rendered_values():
    """The host also runs the ver99 stack as plain subprocesses. Two
    orchestrators on one redis share `orchestrator-group` and its PEL, steal
    each other's pending entries, and spawn duplicate workers for one bug —
    silent corruption, not a crash.

    The check must read the RENDERED value: the overlay normally doesn't
    mention REDIS_URL at all (it inherits values.yaml), so grepping the
    overlay file would be a check that can only ever pass.
    """
    content = _read(SETUP)
    assert "helm template" in content, (
        "the redis-isolation check must render the merged values, not grep "
        "the overlay file (which usually doesn't set REDIS_URL at all)"
    )
    assert "redis://redis:" in content


def test_setup_needs_no_interactive_sudo():
    """setup.sh is routinely run over ssh / in the background, where a sudo
    password prompt hangs with no output. Anything needing root must print
    the command and exit 10 (distinct from 4 = pre-flight) instead."""
    content = _read(SETUP)
    assert "exit 10" in content
    assert "sudo install" in content, (
        "the kubeconfig bootstrap must be printed for the operator to run"
    )


def test_crossnode_check_does_not_handcraft_a_job_spec():
    """A6b must drive the REAL webhook → orchestrator → spawner chain.

    Hand-writing a Job would duplicate K8sJobSpawner._build_job's spec, and a
    hand-copied spec keeps passing after the real one changes — testing
    something that no longer exists. Constrain placement instead: drop the
    taint, cordon the server node, let the real chain place the worker.
    """
    content = _read(CROSSNODE)
    assert "kind: Job" not in content, "crossnode-check must not hand-write a Job spec"
    assert "cordon" in content and "taint node" in content
    assert "pipeline?ref=main" in content, (
        "the only way to spawn a worker is a failed-pipeline webhook"
    )


def test_crossnode_check_restores_node_state_and_verifies_it():
    """Leaving the server node cordoned would break every later deploy in a
    way that looks unrelated to this script. Issuing `uncordon` is not the
    same as it having taken effect, so the restore path reads the state back.
    """
    content = _read(CROSSNODE)
    assert "trap restore EXIT INT TERM" in content
    assert "RESTORE INCOMPLETE" in content, (
        "restore must verify by reading node state back, and say so loudly "
        "when it did not take"
    )


def test_crossnode_check_orders_diagnosis_before_blaming_the_network():
    """Every failure mode here looks identical from outside — 'no MR' — and
    the tempting story is always 'the ocean link is bad'. The script must
    check our own side first, in causal order."""
    content = _read(CROSSNODE)
    for probe in ("BugReportedEvent", "get pods -l app=bf-worker -o wide",
                  "describe pod"):
        assert probe in content, f"missing diagnostic step: {probe}"
    assert content.index("BugReportedEvent") < content.index("suspect the cross-ocean")


def test_crossnode_check_asserts_the_worker_actually_landed_remotely():
    """An MR proves the pipeline worked; it does not prove anything crossed
    an ocean. The node the worker pod ran on is the core assertion."""
    content = _read(CROSSNODE)
    assert 'WORKER_NODE" = "$REMOTE_NODE' in content or \
           '"$WORKER_NODE" = "$REMOTE_NODE"' in content
    assert "Nothing cross-ocean was proven" in content


def test_k3s_smoke_delegates_to_the_shared_script():
    """The acceptance criteria live in infra/k8s/gitlab-smoke.sh. A forked
    copy is a forked criterion, so the k3s wrapper only overrides endpoint /
    project / kubeconfig and execs the shared script."""
    content = _read(SMOKE)
    assert "exec bash infra/k8s/gitlab-smoke.sh" in content
    assert "GITLAB_API" in content


def test_shared_smoke_script_keeps_its_gitlab_com_default():
    """Parameterising GITLAB_API must not change the kind/gitlab.com path."""
    shared = (K3S.parent / "k8s" / "gitlab-smoke.sh").read_text()
    assert 'GITLAB_API="${GITLAB_API:-https://gitlab.com/api/v4}"' in shared


def test_k3s_uses_its_own_per_host_overlay_file():
    """The k3s harness must NOT auto-layer the kind harness's
    values.local.yaml.

    Found live on ls4900 (2026-08-02): that file is kind-era per-host state —
    a Grafana Ingress with ingressClassName: nginx and
    root_url http://localhost:18080/grafana. On the k3s cluster there is no
    ingress controller (traefik disabled) and :18080 died with the kind
    cluster, so layering it installs a resource nothing reconciles pointing at
    a URL that does not exist. It happened to be inert only because the k3s
    overlay ships monitoring.enabled=false — it would go live the moment
    monitoring is switched on.

    Same class of mistake as sharing ~/.kube/config across harnesses: per-host
    state must not be shared between deployment shapes.
    """
    code = _code(SETUP)
    assert "values.local-k3s.yaml" in code, (
        "setup.sh must default LOCAL_VALUES_FILE to a k3s-specific per-host "
        "overlay"
    )
    assert "values.local.yaml}" not in code, (
        "setup.sh must not default to the kind harness's values.local.yaml"
    )


def test_setup_waits_for_every_deployment_it_restarts():
    """Restarting two Deployments and waiting on one leaves the other
    mid-swap when the placement summary prints — two pods per Deployment,
    which reads like a scheduling bug that isn't there."""
    code = _code(SETUP)
    assert "for d in orchestrator gateway; do" in code, (
        "both restarted Deployments must be waited on before the summary"
    )


def test_setup_tolerates_an_unreachable_registry_for_third_party_images():
    """docker.io is routinely unreachable from the CN host while the daemon
    already has the image. A raw `docker pull` failure printed mid-run reads
    like a build failure; load-image.sh is the real guard (it inspects every
    image before importing)."""
    code = _code(SETUP)
    assert "docker image inspect redis:7-alpine" in code, (
        "a failed pull must fall back to checking the local daemon"
    )


def test_load_image_remote_default_is_worker_only():
    """A remote node only ever runs bf-worker Jobs.

    Every service Deployment is pinned to the server node by the overlay's
    nodeSelector, so defaulting the remote target to the full five-image set
    would push ~1 GB across an ocean for images nothing there can schedule.
    The local target still needs all of them.
    """
    code = _code(LOAD_IMAGE)
    assert "DEFAULT_REMOTE=(dh-bf-worker:latest)" in code, (
        "remote default must be bf-worker only"
    )
    assert "DEFAULT_LOCAL=" in code and "dh-gateway" in code, (
        "local default must still cover every service image"
    )


# ── W4: ver99 mode + taint convergence ─────────────────────────────────────

def test_taint_step_converges_in_both_directions():
    """W3 turned the cross-continent taint ON; W4 (--ver99) turns it OFF.

    Both directions have to be a re-run of setup.sh rather than a hand-edited
    cluster, or the two modes drift apart on the one host that has them. The
    removal form is `key-`, which is a no-op when the taint is already absent —
    that's what makes the OFF direction idempotent too.
    """
    code = _code(SETUP)
    assert 'if [ -n "$CROSS_TAINT" ]; then' in code, (
        "the taint step must branch on CROSS_TAINT rather than always tainting"
    )
    assert '"${CROSS_TAINT_KEY}-"' in code, (
        "removing the taint needs the `key-` form (idempotent when absent)"
    )
    # `${CROSS_TAINT-default}` (no colon) so an explicitly EMPTY value means
    # "converge to absent" instead of falling back to the default.
    assert "CROSS_TAINT=\"${CROSS_TAINT-" in code, (
        "CROSS_TAINT must use ${VAR-default}, not ${VAR:-default}: an empty "
        "value is a deliberate instruction, not an unset one"
    )


def test_ver99_flag_couples_its_three_changes():
    """--ver99 must do all three or none.

    Layering the overlay without dropping the taint gives a cluster that looks
    ver99-enabled and can only ever use one node; dropping the taint without
    the overlay gives workers no docker.sock and no resources. Both read as
    "configured".
    """
    code = _code(SETUP)
    assert "--ver99) VER99=1" in code
    assert 'if [ "$VER99" = 1 ]; then\n  CROSS_TAINT=""' in code, (
        "--ver99 must clear the taint"
    )
    assert "VER99_VALUES_FILE" in code and "HELM_VALUES_ARGS+=(-f \"$VER99_VALUES_FILE\")" in code, (
        "--ver99 must layer the ver99 values file"
    )


def test_ver99_refuses_a_placeholder_or_latest_worker_image():
    """The image tag is load-bearing, not cosmetic.

    imagePullPolicy is IfNotPresent and images are side-loaded with
    `k3s ctr images import` — there is no registry to re-pull from. Under a
    moving tag the two nodes can hold different content with nothing to reveal
    it, so setup refuses `:latest` outright, and refuses the shipped
    placeholder rather than deploying something that cannot start.
    """
    code = _code(SETUP)
    assert "*REPLACE_ME|\"\")" in code, "must refuse the placeholder tag"
    assert "*:latest)" in code, "must refuse a :latest worker image"
    assert "docker image inspect \"$WORKER_IMAGE_TAG\"" in code, (
        "must verify the image exists locally before deploying"
    )


def test_ver99_checks_the_remote_node_has_the_image():
    """A worker that lands on a node without the image ImagePullBackOffs, and
    that failure is SILENT: the Job neither succeeds nor fails, so the
    orchestrator only ever sees a warmup timeout with nothing naming the cause.

    Checked via `node.status.images` (no ssh) and reported as a warning rather
    than an automatic ~1.5 GB cross-ocean transfer on every idempotent re-run.
    """
    code = _code(SETUP)
    assert "status.images" in code, (
        "use node.status.images to check remote image presence without ssh"
    )
    assert "load-image.sh --node $REMOTE_NODE" in code, (
        "the warning must name the exact fix"
    )


def test_load_image_picks_up_swebench_tags_automatically():
    """The ver99 image carries an immutable tag, so it cannot be a hardcoded
    default — but forgetting it on one node is exactly the silent
    ImagePullBackOff above. Discover whatever is built locally instead."""
    code = _code(LOAD_IMAGE)
    assert "dh-bf-worker-swebench:" in code
    assert "DEFAULT_REMOTE+=(" in code and "DEFAULT_LOCAL+=(" in code, (
        "discovered swebench tags must be added to BOTH targets"
    )
    assert "grep -v ':latest$'" in code, (
        "never ship the convenience :latest alias to a node"
    )


def test_crossnode_check_surfaces_the_journal_blind_spot():
    """The journal is a node-local hostPath and the exporter is pinned to the
    server node, so a cross-node run is invisible to Grafana —
    `sdlcma_runs_total` is structurally low, not wrong. W4 does not fix that
    (it is W5/W6); it must at least stop people reading the dashboard as if it
    covered both nodes."""
    code = _code(CROSSNODE)
    assert "Do not use Grafana to judge cross-node runs" in code
    assert "/var/sdlcma/journal" in code


def test_crossnode_check_counts_orphaned_eval_containers():
    """mini's evaluation container outliving its worker is BY DESIGN (it is
    what makes resume possible), but at 15-way concurrency it can hold tens of
    GB for two hours. No janitor — a wrong guess kills a run mid-recovery — so
    visibility is the whole mitigation."""
    code = _code(CROSSNODE)
    assert "--filter name=minisweagent-" in code


# ── W4: build-swebench-image.sh portability ────────────────────────────────
#
# Every one of these failed on the FIRST real run on ls4900 and could not have
# failed on the dev box, because the dev box has git history, pip, and a quiet
# import. A deploy host has none of the three.

BUILD_SWEBENCH = K3S / "build-swebench-image.sh"


def test_build_script_silences_the_mini_banner():
    """minisweagent prints a banner + "Loading global config" to STDOUT on
    import, so a bare command substitution captures it as part of the path."""
    code = _code(BUILD_SWEBENCH)
    assert "MSWEA_SILENT_STARTUP=1" in code


def test_build_script_gets_provenance_without_local_git():
    """On ls4900 neither ~/sdlcma nor ~/mini-swe-agent is a git checkout — both
    are rsync'd trees. The fix is NOT to relax the "no empty label" rule (an
    authoritative-looking empty label is worse than none), but to take the
    commit from a stamp the source machine wrote at deploy time."""
    code = _code(BUILD_SWEBENCH)
    assert ".sdlcma-provenance" in code
    assert "git rev-parse --git-dir" in code, "must probe for git before using it"
    # …and still refuse when nothing supplies it.
    assert "Refusing to build an image whose" in code


def test_build_script_does_not_hardcode_pip():
    """The deploy host's venvs come from `uv venv`, which installs no pip, and
    its system python3 has none either — so `pip wheel` fails on exactly the
    machine this script exists for."""
    code = _code(BUILD_SWEBENCH)
    assert "uv build --wheel" in code, "need a uv path when pip is absent"
    assert 'command -v uv' in code


def test_build_script_does_not_swallow_the_wheel_log():
    """First failure on ls4900 printed only "pip wheel failed"; the actual
    cause ("No module named pip") was in the discarded output."""
    code = _code(BUILD_SWEBENCH)
    assert 'tail -25 "$WHEEL_LOG"' in code


def test_build_script_refuses_a_moving_tag():
    """The tag must identify the build; :latest is only a local alias."""
    code = _code(BUILD_SWEBENCH)
    assert 'TAG="dh-bf-worker-swebench:${SHA}"' in code
    assert "SDLCMA_COMMIT" in code


def test_load_image_can_skip_when_already_present_without_sudo():
    """On a host whose sudo is password-gated (ls4900), the import step returns
    10 every time — including right after the operator imported by hand — so
    setup.sh could never complete non-interactively.

    The presence check therefore has to work WITHOUT sudo, which rules out
    `k3s ctr images ls`. `node.status.images` answers it through the API.
    """
    code = _code(LOAD_IMAGE)
    assert "_already_on_local_node" in code
    assert "status.images" in code
    assert "k3s ctr images ls" not in code, (
        "the presence check must not need sudo — that is the whole point"
    )


def test_image_presence_checks_handle_containerd_name_normalisation():
    """containerd rewrites names on import: `dh-bf-worker:latest` is stored and
    reported as `docker.io/library/dh-bf-worker:latest`.

    Two ways to get this wrong, both hit on the first real run:
      * `{...names}` emits one JSON ARRAY per image (brackets, quotes) — only
        `{...names[*]}` flattens to one bare name per token;
      * an exact compare against the unqualified name never matches.
    Failing closed made it merely useless rather than dangerous, but useless is
    still the whole feature gone.
    """
    for script in (LOAD_IMAGE, SETUP):
        code = _code(script)
        if "status.images" not in code:
            continue
        assert "status.images[*].names[*]" in code, (
            f"{script.name} must flatten with names[*], or it compares against "
            "JSON array literals"
        )
        assert ('/$img' in code) or ("(^|/)" in code), (
            f"{script.name} must tolerate the registry-qualified form"
        )


def test_ver99_image_tag_is_resolved_before_it_is_used():
    """The tag must be resolved up front, not lazily at the helm step.

    Real failure: the import step (which checks whether the remote node has the
    image) referenced WORKER_IMAGE_TAG while it was still assigned twenty steps
    later. Under `set -u` that is a hard stop — and by then the run had ALREADY
    removed the cross-continent taint, i.e. it half-applied a mode change and
    died. Anything the later steps consume has to be resolved in a pre-flight.
    """
    code = _code(SETUP)
    resolve = code.index("WORKER_IMAGE_TAG=$(grep -oE")
    first_use = min(
        code.index("grep -qE \"(^|/)${WORKER_IMAGE_TAG}"),
        code.index('say "  layering $VER99_VALUES_FILE'),
    )
    assert resolve < first_use, (
        "WORKER_IMAGE_TAG is used before it is assigned — with set -u that "
        "aborts mid-run, after earlier steps already mutated the cluster"
    )


def test_presence_skip_never_applies_to_a_moving_tag():
    """Skipping the import for a `:latest` image is unsound, and the failure it
    produces is the worst kind: the node keeps the PREVIOUS build while the new
    config is applied, so the deployment looks successful and runs old code.

    Observed on the first ver99 bring-up — the orchestrator came up pre-W4
    while every K8S_WORKER_* variable was correctly set, because the image had
    been rebuilt after the operator's manual import. Only an immutable tag can
    be verified by name.
    """
    code = _code(LOAD_IMAGE)
    fn = code[code.index("_already_on_local_node()"):]
    fn = fn[:fn.index("\n}")]
    assert "*:latest|*-dirty) return 1" in fn, (
        "_already_on_local_node must refuse to vouch for a moving tag — and "
        "`-dirty` is one: it names 'some uncommitted state of <sha>', which the "
        "next rebuild reuses with different content"
    )


def test_setup_has_an_explicit_skip_images_escape_hatch():
    """A config-only re-run must not cost a 2 GB save/import plus a human.

    The import step deliberately refuses to vouch for moving tags without
    sudo, which is correct — but on a password-gated host it makes EVERY
    re-run interactive, including ones that only change a ConfigMap value.
    `--skip-images` is the operator asserting the images are unchanged. It is
    an assertion, not a check, so it must be typed explicitly and can never be
    inferred (a wrong inference here is how you deploy old code).
    """
    code = _code(SETUP)
    assert "--skip-images) SKIP_IMAGES=1" in code
    assert 'if [ "$SKIP_IMAGES" = 1 ]; then' in code
    # and it must be OFF by default
    assert "SKIP_IMAGES=0" in code
