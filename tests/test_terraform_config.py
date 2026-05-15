"""Static validation for the 1a.5 Terraform config (infra/terraform).

Two layers:
  1. Tool checks — `terraform fmt -check` (no providers/network needed) and
     `terraform validate` (best-effort: skipped if the working dir isn't
     initialized and `init` can't reach the registry, e.g. offline CI).
  2. Source invariants — grep-level assertions that encode the design
     contract decided in 1a.5 so a future edit can't silently break it:
     Terraform must NOT manage the Secret, must own the namespace, and must
     install the chart with namespace.create=false / wait defaulting false.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TF_DIR = REPO_ROOT / "infra" / "terraform"

pytestmark = pytest.mark.skipif(
    shutil.which("terraform") is None, reason="terraform not on PATH"
)


def _tf(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["terraform", *args],
        cwd=TF_DIR,
        capture_output=True,
        text=True,
        check=check,
    )


def _src() -> str:
    return "\n".join(p.read_text() for p in sorted(TF_DIR.glob("*.tf")))


# ── tool checks ──────────────────────────────────────────────────

def test_terraform_fmt_clean():
    # -check exits non-zero if any file would be reformatted.
    _tf("fmt", "-check", "-recursive")


def test_terraform_validate():
    initialized = (TF_DIR / ".terraform").is_dir()
    if not initialized:
        init = _tf("init", "-backend=false", "-input=false", check=False)
        if init.returncode != 0:
            pytest.skip("terraform init failed (likely offline); skipping validate")
    res = _tf("validate", check=False)
    assert res.returncode == 0, res.stdout + res.stderr


# ── source invariants (design contract) ──────────────────────────

def test_terraform_does_not_manage_the_secret():
    src = _src()
    # The Secret stays out-of-band (1a.2–1a.5 stance): no Secret-bearing
    # resource, and no credential *variables* that would land in tfstate.
    # (A plaintext `kubectl create secret …` reminder in an output string is
    # fine — it carries no values and never enters cluster/state.)
    assert 'resource "kubernetes_secret"' not in src
    assert 'resource "kubernetes_manifest"' not in src  # no smuggling one through
    assert "sensitive   = true" not in src and "sensitive = true" not in src
    for cred in ("llm_api_key", "gitlab_private_token", "ssh_private_key"):
        assert f'variable "{cred}"' not in src.lower()


def test_terraform_owns_namespace_and_helm_release():
    src = _src()
    assert 'resource "kubernetes_namespace" "sdlcma"' in src
    assert 'resource "helm_release" "sdlcma"' in src
    assert "../helm/sdlcma" in src  # release points at the 1a.4 chart


def test_helm_release_does_not_double_own_namespace():
    src = _src()
    assert "create_namespace = false" in src
    # chart value namespace.create is forced false (TF owns the ns)
    assert 'name  = "namespace.create"' in src
    assert 'value = "false"' in src


def test_helm_wait_defaults_false():
    # Apply must not block on the orchestrator pod, which is intentionally
    # not Ready until the operator creates the out-of-band Secret.
    vars_tf = (TF_DIR / "variables.tf").read_text()
    assert 'variable "helm_wait"' in vars_tf
    block = vars_tf.split('variable "helm_wait"', 1)[1].split("variable ", 1)[0]
    assert "default     = false" in block


def test_provider_lock_file_is_tracked():
    # .terraform.lock.hcl must be committed (pinned providers); only the
    # .terraform/ dir + state are gitignored.
    assert (TF_DIR / ".terraform.lock.hcl").is_file()
    lines = (REPO_ROOT / ".gitignore").read_text().splitlines()
    active = [ln.strip() for ln in lines
              if ln.strip() and not ln.strip().startswith("#")]
    assert "infra/terraform/*.tfstate" in active
    assert "infra/terraform/.terraform/" in active
    # no active rule ignores the lock file (mentions in comments are fine)
    assert not any("lock.hcl" in ln for ln in active)
