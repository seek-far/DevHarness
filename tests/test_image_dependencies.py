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
