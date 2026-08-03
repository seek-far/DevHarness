"""Guard: what a component's image IMPORTS must be what its image INSTALLS,
and what a component's command RUNS must be what its image COPIES.

Incident provenance (2026-05-31, K8s gitlab@minus bring-up): `prometheus-
client` was added to the *top-level* requirements.txt for the new /metrics
surfaces — but every service image installs only its OWN per-component
requirements (each `Dockerfile.*` does `COPY <component>/requirements.txt`
+ `pip install -r` that file; project rule: component-scoped deps, never a
shared monolith). Result: orchestrator / gateway / llm-gateway CrashLooped
with `ModuleNotFoundError: No module named 'prometheus_client'`. Separately
runrecord-exporter (reuses the bf-worker image, command `python -m
tools.runrecord_exporter`) CrashLooped because Dockerfile.bf-worker never
COPYd `tools/` → `No module named 'tools'`.

The general lesson, made executable here so it can't recur silently:

  * Adding a runtime dependency to the top-level requirements.txt propagates
    to NO image. Each component that imports a cross-cutting package must
    declare it in the requirements.txt its own Dockerfile installs.
    `test_cross_cutting_imports_are_declared` derives (installed-requirements,
    copied-source-dirs) straight from each Dockerfile and fails if any COPYd
    source imports a tracked package the image doesn't install. Add new
    cross-cutting runtime deps to CROSS_CUTTING below when they appear.

  * A `python -m pkg.module` entrypoint only works if `pkg/` is actually in
    the image. `test_runrecord_exporter_module_is_in_its_image` ties the helm
    Deployment's command to the image's COPY set.

These are static lint-style guards — no docker/cluster needed.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

# import-name (as written in `import X` / `from X import`) → distribution
# substring that must appear in the component's requirements.txt. Grow this
# when a new cross-cutting runtime package starts being imported by more than
# one component image.
CROSS_CUTTING = {
    "prometheus_client": "prometheus-client",
    # Entra ID / Managed Identity auth for Azure LLM backends. Imported lazily
    # (inside a function) by bf_worker/services/azure_auth.py, and by
    # llm_gateway once its Azure backend lands — the lazy import keeps
    # non-Azure deployments working without the package, but any image that
    # BUNDLES the importing source must still install it, or a run with
    # llm_auth_mode=entra dies at first LLM call.
    "azure.identity": "azure-identity",
}


def _dockerfiles() -> list[Path]:
    return sorted(REPO.glob("Dockerfile.*"))


def _parse_dockerfile(text: str) -> tuple[list[str], list[str]]:
    """Return (installed requirements paths, COPYd build-context source dirs).

    requirements: every `-r <…requirements.txt>` argument to a pip install.
    source dirs:  every `COPY <src>/ <dest>` whose src is a directory in the
                  build context (we only care about first-party source trees).
    """
    reqs = re.findall(r"-r\s+(\S+requirements\.txt)", text)
    copied = re.findall(r"^COPY\s+(\S+?)/\s+\S+", text, re.MULTILINE)
    return reqs, copied


def _imports_package(source_dir: Path, import_name: str) -> Path | None:
    """First *.py under source_dir that imports import_name, else None."""
    pat = re.compile(
        rf"^\s*(?:from\s+{re.escape(import_name)}[\s.]|import\s+{re.escape(import_name)}\b)",
        re.MULTILINE,
    )
    if not source_dir.is_dir():
        return None
    for py in source_dir.rglob("*.py"):
        if pat.search(py.read_text(encoding="utf-8", errors="ignore")):
            return py
    return None


@pytest.mark.parametrize("dockerfile", _dockerfiles(), ids=lambda p: p.name)
def test_cross_cutting_imports_are_declared(dockerfile: Path) -> None:
    """Every cross-cutting package a component's COPYd source imports must be
    declared in the requirements.txt that component's Dockerfile installs."""
    text = dockerfile.read_text(encoding="utf-8")
    req_paths, source_dirs = _parse_dockerfile(text)

    declared = "\n".join(
        (REPO / r).read_text(encoding="utf-8")
        for r in req_paths
        if (REPO / r).is_file()
    )

    for import_name, dist in CROSS_CUTTING.items():
        importer = next(
            (
                hit
                for d in source_dirs
                if (hit := _imports_package(REPO / d, import_name)) is not None
            ),
            None,
        )
        if importer is None:
            continue  # this image doesn't import the package — nothing required
        assert dist in declared, (
            f"{dockerfile.name} bundles {importer.relative_to(REPO)} which imports "
            f"`{import_name}`, but none of its installed requirements "
            f"({req_paths or 'NONE'}) declare `{dist}`. Per-component images "
            f"install their OWN requirements.txt — adding `{dist}` to the "
            f"top-level requirements.txt does NOT reach this image."
        )


def test_runrecord_exporter_module_is_in_its_image() -> None:
    """The runrecord-exporter Deployment runs `python -m tools.runrecord_exporter`
    on the bf-worker image (values.yaml: 'Reuses the bf-worker image'). That
    only works if Dockerfile.bf-worker COPYs the `tools/` package."""
    tmpl = (
        REPO / "infra" / "helm" / "sdlcma" / "templates" / "runrecord-exporter.yaml"
    ).read_text(encoding="utf-8")
    m = re.search(r'python",\s*"-m",\s*"([\w.]+)"', tmpl)
    assert m, "runrecord-exporter template must run a `python -m <module>` command"
    top_pkg = m.group(1).split(".")[0]  # tools.runrecord_exporter → tools

    bf_worker_df = (REPO / "Dockerfile.bf-worker").read_text(encoding="utf-8")
    assert re.search(rf"^COPY\s+{re.escape(top_pkg)}/\s", bf_worker_df, re.MULTILINE), (
        f"runrecord-exporter runs `python -m {m.group(1)}` on the bf-worker image, "
        f"but Dockerfile.bf-worker does not `COPY {top_pkg}/` into it — the pod "
        f"will crash with `No module named '{top_pkg}'`."
    )


# ── W4: the ver99 worker image ─────────────────────────────────────────────
#
# ver99 in a pod needs three things the base worker image does not have, and
# each one fails LATE and quietly if missing: mini's imports are lazy (so the
# run dies mid-way with fetch_trace/parse_trace already green), the docker CLI
# is only reached from inside mini's environment layer, and configs/ is only
# read once the agent spec is loaded. They live in a SEPARATE image so the
# base one — shared with kind, ECS and docker-compose — stays untouched.

SWEBENCH_DF = REPO / "Dockerfile.bf-worker-swebench"
BASE_DF = REPO / "Dockerfile.bf-worker"


def test_base_worker_image_stays_free_of_ver99_extras() -> None:
    """The additive guarantee for W4 is STRUCTURAL: it is the file boundary,
    not a default value someone can flip.

    If these ever move into Dockerfile.bf-worker, three unrelated deployment
    shapes inherit a ~250 MB image and a build-time dependency on
    download.docker.com (unreachable from the CN host). Fail here instead.
    """
    text = BASE_DF.read_text()
    assert "docker-ce-cli" not in text and "docker/docker" not in text, (
        "the docker CLI belongs in Dockerfile.bf-worker-swebench"
    )
    assert "mini_swe_agent" not in text and "mini-swe-agent" not in text
    assert not re.search(r"^COPY\s+configs/", text, re.MULTILINE), (
        "configs/ belongs in the swebench image only"
    )


def test_swebench_image_layers_on_the_base_image() -> None:
    text = SWEBENCH_DF.read_text()
    assert re.search(r"^FROM \$\{BASE\}", text, re.MULTILINE)
    assert 'ARG BASE=dh-bf-worker:latest' in text


def test_swebench_image_supplies_all_three_missing_pieces() -> None:
    """docker CLI + mini wheel + configs/ + `patch`.

    The first three were found while designing W4 (two of them unrecorded in
    the plan). `patch` only surfaced on the first ver99-in-a-pod run: the apply
    node is a fallback ladder ending in `patch -p1`, and the slim base image
    has none — which additionally destroyed the diagnostics of the two rungs
    that did run (see tests/test_workflow_ver99.py).
    """
    text = SWEBENCH_DF.read_text()
    assert "install -m 0755 /tmp/docker/docker /usr/local/bin/docker" in text
    assert "mini_swe_agent-*.whl" in text
    assert re.search(r"^COPY\s+configs/\s+/app/configs/", text, re.MULTILINE)
    assert re.search(r"apt-get install[^\n]*\bpatch\b", text), (
        "the ver99 apply ladder needs the `patch` binary"
    )


def test_swebench_image_verifies_itself_at_build_time() -> None:
    """Each of the three is otherwise discovered 20 minutes into a sweep:
    mini's imports are lazy, so fetch_trace and parse_trace go green first and
    it looks like a code bug (the same shape as the W3 401 incident)."""
    text = SWEBENCH_DF.read_text()
    assert "command -v docker" in text
    assert "command -v patch" in text
    assert "import minisweagent" in text
    assert "test -f /app/configs/swebench/gitlab_ver99.json" in text


def test_swebench_image_records_provenance_labels() -> None:
    """A tag names the BUILD; the ingredients go in labels.

    This image has three moving parts (sdlcma code, the mini fork, the docker
    CLI), so any tag scheme is selectively silent about two of them. The
    question that actually matters on a two-node cluster — "is this the same
    mini fork the vendored files are pinned to?" — is answerable only from
    here, via `docker inspect` / `crictl inspecti`.
    """
    text = SWEBENCH_DF.read_text()
    for label in ("org.opencontainers.image.revision",
                  "io.sdlcma.mini-commit",
                  "io.sdlcma.mini-version"):
        assert label in text, f"missing provenance label {label}"


def test_wheels_dir_is_not_dockerignored() -> None:
    """The wheel staging dir cannot be build/ or dist/: .dockerignore excludes
    both, so a wheel placed there is invisible to COPY and the build fails with
    a confusing 'no such file'. Hence the separate wheels/.

    Skips when .dockerignore is absent: root dotfiles are untracked in this
    repo (`/.*` in .gitignore), so a checkout or an rsync'd deploy host may
    legitimately not have it — and a test that fails there would be reporting
    on the transport, not on the code.
    """
    path = REPO / ".dockerignore"
    if not path.is_file():
        pytest.skip(".dockerignore not present (untracked root dotfile)")
    dockerignore = path.read_text()
    assert not re.search(r"^wheels/?$", dockerignore, re.MULTILINE), (
        "wheels/ must NOT be in .dockerignore — the swebench image COPYs it"
    )
    assert re.search(r"^build/$", dockerignore, re.MULTILINE), (
        "build/ is expected to stay excluded — that is why wheels/ exists"
    )
