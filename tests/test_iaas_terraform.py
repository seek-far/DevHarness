"""Static validation for the 1b.2 IaaS Terraform layer.

Mirrors test_terraform_config.py's two-layer approach:
  1. Tool checks — `terraform fmt -check` + best-effort `terraform validate`.
  2. Source invariants — encode the 1b.2 design contract so a future edit
     can't silently break it:
       - the IaaS module exposes the provider-neutral contract (CONTRACT.md):
         every contract variable + output is present, by name;
       - the OpenStack impl is pinned to the openstack provider v3 line;
       - the 1b root is a SEPARATE root from the live 1a helm_release root
         (it must not declare the chart/namespace), and the 1a root is
         left intact (still owns them);
       - no secrets committed: no private-key variable, ssh_public_key has
         no default at the root, the new root's state is gitignored.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TF = REPO_ROOT / "infra" / "terraform"
MOD_DIR = TF / "modules" / "iaas" / "openstack"
ROOT_DIR = TF / "iaas-openstack"
CONTRACT_MD = TF / "modules" / "iaas" / "CONTRACT.md"
OLD_ROOT = TF  # the live 1a helm_release root

CONTRACT_VARS = (
    "name_prefix", "control_count", "worker_count", "control_flavor",
    "worker_flavor", "image_name", "ssh_public_key", "ssh_user",
    "network_cidr", "dns_nameservers", "external_network",
)
CONTRACT_OPTIONAL_VARS = ("cloud_init",)
K8S_BOOTSTRAP = REPO_ROOT / "infra" / "k8s-bootstrap"
APP_ROOT = TF / "app-openstack"
HELM_ROOT = TF  # 1a helm root
AWS_MOD = TF / "modules" / "iaas" / "aws"
AWS_ROOT = TF / "iaas-aws"


def _names(src: str, kind: str) -> set[str]:
    # kind = "variable" | "output"
    return set(re.findall(rf'{kind} "([^"]+)"', src))
CONTRACT_OUTPUTS = (
    "control_ips", "worker_ips", "ssh_user", "network_id", "kubeapi_endpoint",
)

pytestmark = pytest.mark.skipif(
    shutil.which("terraform") is None, reason="terraform not on PATH"
)


def _tf(*args: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["terraform", *args], cwd=cwd, capture_output=True, text=True, check=check
    )


def _src(d: Path) -> str:
    return "\n".join(p.read_text() for p in sorted(d.glob("*.tf")))


# ── tool checks ──────────────────────────────────────────────────

def test_iaas_fmt_clean():
    _tf("fmt", "-check", "-recursive", cwd=MOD_DIR)
    _tf("fmt", "-check", "-recursive", cwd=ROOT_DIR)


def test_iaas_root_validate():
    if not (ROOT_DIR / ".terraform").is_dir():
        init = _tf("init", "-backend=false", "-input=false",
                   cwd=ROOT_DIR, check=False)
        if init.returncode != 0:
            pytest.skip("terraform init failed (likely offline)")
    res = _tf("validate", cwd=ROOT_DIR, check=False)
    assert res.returncode == 0, res.stdout + res.stderr


# ── contract: module exposes the provider-neutral seam ───────────

def test_module_exposes_every_contract_variable():
    src = _src(MOD_DIR)
    for v in CONTRACT_VARS:
        assert f'variable "{v}"' in src, f"contract variable {v} missing"


def test_module_exposes_every_contract_output():
    src = _src(MOD_DIR)
    for o in CONTRACT_OUTPUTS:
        assert f'output "{o}"' in src, f"contract output {o} missing"


def test_root_reexports_contract_outputs():
    # Consumers (1b.3/1b.4) read the root, not the module internals.
    src = _src(ROOT_DIR)
    for o in CONTRACT_OUTPUTS:
        assert f'output "{o}"' in src
    assert "../modules/iaas/openstack" in src  # root wires the impl module


def test_contract_doc_is_present():
    txt = CONTRACT_MD.read_text()
    for name in CONTRACT_VARS + CONTRACT_OPTIONAL_VARS + CONTRACT_OUTPUTS:
        assert name in txt, f"{name} undocumented in CONTRACT.md"


# ── 1b.3: optional cloud-init contract + kubeadm bootstrap ───────

def test_module_accepts_optional_cloud_init_with_default():
    # Optional contract: present, defaulted (omittable at the call site),
    # and actually wired into the instance user_data.
    src = _src(MOD_DIR)
    for v in CONTRACT_OPTIONAL_VARS:
        assert f'variable "{v}"' in src
    block = src.split('variable "cloud_init"', 1)[1].split("variable ", 1)[0]
    assert re.search(r'^\s*default\s*=\s*""', block, re.M)
    assert "user_data" in src and "var.cloud_init" in src


def test_root_feeds_cloud_init_file_into_module():
    src = _src(ROOT_DIR)
    assert 'variable "cloud_init_file"' in src
    # file() wins over the raw string, but the module still receives a string.
    assert "file(" in src and "var.cloud_init_file" in src


def test_k8s_bootstrap_artifacts_present_and_sane():
    ci = (K8S_BOOTSTRAP / "cloud-init.yaml").read_text()
    assert ci.startswith("#cloud-config")
    for token in ("containerd", "kubeadm", "kubelet", "br_netfilter",
                  "pkgs.k8s.io"):
        assert token in ci, f"cloud-init missing {token}"

    bs = K8S_BOOTSTRAP / "bootstrap.sh"
    txt = bs.read_text()
    assert bs.stat().st_mode & 0o111, "bootstrap.sh not executable"
    assert "set -euo pipefail" in txt
    assert "kubeadm init" in txt and "print-join-command" in txt
    # idempotency guards (compose with terraform apply re-runs)
    assert "/etc/kubernetes/admin.conf" in txt
    assert "/etc/kubernetes/kubelet.conf" in txt
    # kubeconfig is rewritten to the floating IP and kept out of the repo
    assert "server: https://" in txt
    assert ".kube/sdlcma-1b.config" in txt


# ── provider pin ─────────────────────────────────────────────────

def test_openstack_provider_v3_pinned():
    src = _src(MOD_DIR) + _src(ROOT_DIR)
    assert "terraform-provider-openstack/openstack" in src
    assert 'version = "~> 3.0"' in src


# ── isolation from the live 1a state ─────────────────────────────

def test_1b_root_is_separate_from_1a_helm_root():
    # The 1b IaaS root must NOT manage the chart/namespace — that lives in
    # the 1a root with its own (live) state. Crossing them would risk 1a.
    src = _src(ROOT_DIR)
    assert 'resource "helm_release"' not in src
    assert 'resource "kubernetes_namespace"' not in src


def test_1a_root_left_intact():
    # Regression guard: 1b.2 must not have refactored the live 1a root.
    src = _src(OLD_ROOT)
    assert 'resource "helm_release" "sdlcma"' in src
    assert 'resource "kubernetes_namespace" "sdlcma"' in src


# ── no secrets committed ─────────────────────────────────────────

def _strip_comments(src: str) -> str:
    out = []
    for ln in src.splitlines():
        s = ln.split("#", 1)[0]
        out.append(s)
    return "\n".join(out)


def test_no_private_key_or_password_in_tf():
    # Prose explaining the security stance is fine (cf. test_terraform_config);
    # what must never appear is a credential *variable* or an HCL assignment
    # of private-key / password material.
    code = _strip_comments(_src(MOD_DIR) + _src(ROOT_DIR))
    assert 'variable "ssh_private_key"' not in code
    assert not re.search(r'\bprivate_key\b', code)
    assert not re.search(r'^\s*password\s*=', code, re.M)


def test_ssh_public_key_has_no_default_at_root():
    # Force the key to be supplied (tfvars/-var); never a guessed default.
    vars_tf = (ROOT_DIR / "variables.tf").read_text()
    block = vars_tf.split('variable "ssh_public_key"', 1)[1].split(
        "variable ", 1)[0]
    # No `default = ...` attribute (a "No default" note in the description
    # string is exactly what we want and must not trip this).
    assert not re.search(r'^\s*default\s*=', block, re.M)


def test_new_root_state_is_gitignored():
    active = [
        ln.strip() for ln in (REPO_ROOT / ".gitignore").read_text().splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    assert "infra/terraform/iaas-openstack/*.tfstate" in active
    assert "infra/terraform/iaas-openstack/.terraform/" in active
    assert "infra/terraform/iaas-openstack/*.tfvars" in active


# ── 1b.4: cross-node CNI rule + app-on-OpenStack root ────────────

def test_iaas_allows_intra_node_cni_overlay():
    # Regression guard: without an intra-node allow rule the flannel VXLAN
    # (UDP 8472) overlay is dropped between nodes and any cross-node pod
    # traffic (e.g. CoreDNS) silently times out. Must be scoped to the node
    # subnet, not 0.0.0.0/0.
    src = _src(MOD_DIR)
    assert 'resource "openstack_networking_secgroup_rule_v2" "intra_node"' in src
    block = src.split('"intra_node"', 1)[1].split("resource ", 1)[0]
    assert "remote_ip_prefix  = var.network_cidr" in block or \
           "remote_ip_prefix = var.network_cidr" in block
    assert "ingress" in block


def test_app_root_deploys_unchanged_1a_chart():
    # 1b.4 proves the chart/k8s layer is provider-neutral: the SAME chart the
    # 1a root installs, only the kubeconfig differs.
    src = _src(APP_ROOT)
    assert "../../helm/sdlcma" in src                 # same chart dir as 1a
    assert 'resource "helm_release" "sdlcma"' in src
    assert 'resource "kubernetes_namespace" "sdlcma"' in src
    # environment differences are VALUES overrides (set blocks), not chart edits
    assert 'name  = "ingress.enabled"' in src


def test_app_root_targets_1b_cluster_and_is_separate_state():
    vars_tf = (APP_ROOT / "variables.tf").read_text()
    blk = vars_tf.split('variable "kube_config_path"', 1)[1].split(
        "variable ", 1)[0]
    assert "sdlcma-1b.config" in blk                  # the 1b.3 kubeconfig
    active = [
        ln.strip() for ln in (REPO_ROOT / ".gitignore").read_text().splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    assert "infra/terraform/app-openstack/*.tfstate" in active
    assert "infra/terraform/app-openstack/.terraform/" in active


def test_app_root_does_not_touch_the_chart_templates():
    # The app root must not contain chart YAML — it only references the chart
    # path and overrides values; the chart stays byte-unchanged.
    for p in APP_ROOT.rglob("*"):
        if p.is_file() and ".terraform" not in p.parts:
            assert p.suffix not in (".yaml", ".yml"), f"unexpected chart-like file {p}"


# ── 1b.5: locked contract — openstack ⇄ aws parity ───────────────

def test_aws_module_contract_parity_with_openstack():
    """The capstone: the aws and openstack IaaS modules expose byte-identical
    variable and output NAME sets. This is the locked seam — phase-2 is a
    module `source` swap, nothing else. If this fails the contract drifted."""
    os_src, aws_src = _src(MOD_DIR), _src(AWS_MOD)
    os_vars, aws_vars = _names(os_src, "variable"), _names(aws_src, "variable")
    os_outs, aws_outs = _names(os_src, "output"), _names(aws_src, "output")

    assert os_vars == aws_vars, (
        f"variable contract drift: only-openstack={os_vars - aws_vars}, "
        f"only-aws={aws_vars - os_vars}")
    assert os_outs == aws_outs, (
        f"output contract drift: only-openstack={os_outs - aws_outs}, "
        f"only-aws={aws_outs - os_outs}")
    # and they really are the documented contract
    assert os_vars == set(CONTRACT_VARS) | set(CONTRACT_OPTIONAL_VARS)
    assert os_outs == set(CONTRACT_OUTPUTS)


def test_aws_root_swap_is_source_only():
    # The aws root's module call must be argument-identical to the openstack
    # root's — only `source` differs.
    def _module_args(src: str) -> set[str]:
        blk = src.split('module "iaas"', 1)[1].split("}", 1)[0]
        return set(re.findall(r'^\s*([a-z_]+)\s*=', blk, re.M)) - {"source"}
    assert _module_args(_src(ROOT_DIR)) == _module_args(_src(AWS_ROOT))
    assert '"../modules/iaas/aws"' in _src(AWS_ROOT)
    assert '"../modules/iaas/openstack"' in _src(ROOT_DIR)


def test_aws_fmt_clean_and_validates():
    _tf("fmt", "-check", "-recursive", cwd=AWS_MOD)
    _tf("fmt", "-check", "-recursive", cwd=AWS_ROOT)
    if not (AWS_ROOT / ".terraform").is_dir():
        init = _tf("init", "-backend=false", "-input=false",
                   cwd=AWS_ROOT, check=False)
        if init.returncode != 0:
            pytest.skip("terraform init failed (likely offline)")
    res = _tf("validate", cwd=AWS_ROOT, check=False)
    assert res.returncode == 0, res.stdout + res.stderr


def test_aws_root_state_gitignored_and_key_has_no_default():
    active = [
        ln.strip() for ln in (REPO_ROOT / ".gitignore").read_text().splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    assert "infra/terraform/iaas-aws/*.tfstate" in active
    assert "infra/terraform/iaas-aws/.terraform/" in active
    blk = (AWS_ROOT / "variables.tf").read_text().split(
        'variable "ssh_public_key"', 1)[1].split("variable ", 1)[0]
    assert not re.search(r'^\s*default\s*=', blk, re.M)
