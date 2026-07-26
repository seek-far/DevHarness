#!/usr/bin/env python3
"""
setup_instance.py — build + push one SWE-bench instance branch to GitLab.

The branch-per-instance substrate for the ver==99 GitLab-integrated workflow
(docs/swebench.md). For each instance it creates (idempotently) a per-project
GitLab repo `swebench/<repo>` and a branch `instance/<instance_id>` parked at the
base_commit, carrying:
  - the upstream source tree at base_commit  (extracted from the eval docker
    image via `git archive HEAD` — HEAD is the empty SWE-bench commit whose tree
    == base_commit, so no GitHub clone is needed)
  - .gitlab-ci.yml            per-instance eval image + per-repo test command;
                              fix = `git diff origin/<instance-branch>...HEAD`
                              (empty on the branch itself = red baseline, the
                              worker's mini diff on auto/bf = green)
  - .swebench/instance.json   problem_statement + image + instance_id (+FAIL/PASS)
  - .swebench/test_patch.diff  the oracle test patch CI applies

Credentials: reads GITLAB_API + GITLAB_PRIVATE_TOKEN (+ GITLAB_USERNAME) from the
environment — source your settings/<worker>.env first. The token is only sent in
request headers / the push URL and never printed.

Usage:
    export $(grep -E '^(GITLAB_API|GITLAB_PRIVATE_TOKEN|GITLAB_USERNAME)=' \
             settings/worker_local_multi_process.env | xargs)
    python infra/swebench-gitlab/setup_instance.py --instance sympy__sympy-22914

Per-repo CI recipe: only repos whose test-runner exit-code semantics have been
verified are supported (M0a finding: sympy uses `bin/test`, not pytest, and its
exit code is reliable). Add a repo to REPO_CI only after verifying its runner
returns non-zero on a failing test — otherwise the red baseline is a false green.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import urllib.parse
from pathlib import Path

DATASET_MAPPING = {
    "verified": "princeton-nlp/SWE-Bench_Verified",
    "lite": "princeton-nlp/SWE-Bench_Lite",
    "full": "princeton-nlp/SWE-Bench",
}


def gitlab_project_path(repo: str) -> str:
    """`sympy/sympy` → `swebench/sympy`. Uses the upstream repo's short name so
    all instances of one project share a single GitLab repo (git dedups history)."""
    return f"swebench/{repo.split('/')[-1]}"


def eval_test_cmd(instance: dict) -> str:
    """The authoritative per-instance test command, extracted from SWE-bench's own
    eval script (`make_test_spec`). It is the line between the '>>>>> Start Test
    Output' / 'End Test Output' markers — SWE-bench has already resolved the
    per-repo runner, the test-target format (pytest file / django dotted module /
    sympy file + PYTHONWARNINGS / sphinx tox), and any version-specific flags. We
    reuse it verbatim so we don't re-implement 12 repos' worth of nuance.

    Offline: `make_test_spec` builds the ENV setup script too, which downloads the
    repo's environment.yml / requirements.txt from raw.githubusercontent.com — a
    network dependency that fails (ConnectionReset) on a CN host without GitHub
    (branch-build failures observed with the tailnet exit-node off). We only use
    the eval/test-command portion of the spec, not the env script, so short-
    circuit those two downloads to a stub. The test command comes from SWE-bench's
    static MAP_REPO_VERSION_TO_SPECS and is unaffected. Same offline principle as
    `grade.py`'s `_SpecShim`."""
    import swebench.harness.test_spec.python as _py
    _py.get_environment_yml_by_commit = lambda *a, **k: "name: testbed\ndependencies: []\n"
    _py.get_requirements_by_commit = lambda *a, **k: ""
    from swebench.harness.test_spec.test_spec import make_test_spec

    ts = make_test_spec(instance)
    lines = ts.eval_script_list
    out, capture = [], False
    for ln in lines:
        if "Start Test Output" in ln:
            capture = True
            continue
        if "End Test Output" in ln:
            break
        if capture:
            out.append(ln)
    cmd = "\n".join(out).strip()
    if not cmd:
        raise SystemExit(f"could not extract test command for {instance['instance_id']}")
    return cmd


def run_tests_sh(instance: dict) -> str:
    """The test-run script committed as `.swebench/run_tests.sh`. Wraps SWE-bench's
    authoritative test command in the Start/End markers grade.py parses. Kept in a
    file (not inlined in YAML) so arbitrary quoting in the test command — e.g.
    sympy's `PYTHONWARNINGS='...'` single quotes — can't break the .gitlab-ci.yml."""
    return (
        "#!/usr/bin/env bash\n"
        "# runs in the testbed conda env (CI activates it before calling this)\n"
        'echo ">>>>> Start Test Output"\n'
        f"{eval_test_cmd(instance)}\n"
        'echo ">>>>> End Test Output"\n'
    )


def build_gitlab_ci_yaml(instance: dict, image: str, branch: str, test_patch: str) -> str:
    """CI: apply the fix to /testbed, apply the oracle test_patch, run the test
    script (`.swebench/run_tests.sh` → SWE-bench's authoritative command, output
    captured with markers), then grade via `.swebench/grade.py` (SWE-bench's OWN
    parser). Pipeline is green iff grade.py exits 0 (== RESOLVED), matching the
    official harness for ALL repos — no per-runner exit-code assumption (django's
    whole-module run can exit 1 on an unrelated error yet be resolved)."""
    return f"""# SWE-bench instance CI — {instance['instance_id']} (workflow_ver==99).
# Job container = the per-instance eval image; /testbed baked at base_commit.
# fix = git diff(instance-branch .. HEAD): empty on the instance branch (red
# baseline), the worker's mini diff on auto/bf (green). CI IS the oracle,
# graded by SWE-bench's own parser (.swebench/grade.py) — matches the harness.
stages: [test]
test:
  image: {image}
  stage: test
  variables:
    FROM_REF: "{branch}"
    GIT_DEPTH: "0"
    # UTF-8 locale: some repos' test output contains non-ASCII (e.g. django
    # prints '…'); without this the runner's ascii default raises
    # UnicodeEncodeError mid-run, collapsing the whole test run (all PASS_TO_PASS
    # read as failed → false unresolved). SWE-bench's harness sets this.
    LC_ALL: "C.UTF-8"
    LANG: "C.UTF-8"
    PYTHONIOENCODING: "utf-8"
    # Deterministic test execution: PYTHONHASHSEED pins set/dict iteration and
    # unittest test-discovery order so the CI baseline is STABLE (always red, not
    # sometimes-green on order-sensitive instances like django-7530 — a false-
    # green baseline means the orchestrator never spawns a worker and the
    # instance times out with zero LLM calls). PYTHONUNBUFFERED makes stdout
    # unbuffered so pytest output interleaves with stderr in a stable order.
    # These match what build_sb_environment injects into mini's container so the
    # agent's observation and CI's grading observe the same deterministic world.
    PYTHONHASHSEED: "0"
    PYTHONUNBUFFERED: "1"
  script:
    - source /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed
    - git fetch -q origin "$FROM_REF" || true
    - git diff "origin/${{FROM_REF}}...HEAD" > /tmp/fix.diff || true
    - cd /testbed && git checkout -- .
    - 'if [ -s /tmp/fix.diff ]; then git apply --3way /tmp/fix.diff && echo "[fix applied]"; else echo "[no fix — baseline, expect RED]"; fi'
    - python -m pip install -e . -q || true
    - git apply "$CI_PROJECT_DIR/.swebench/test_patch.diff" && echo "[test_patch applied]"
    # authoritative test run in the testbed env (py3.9) via the committed script
    - set +e; bash "$CI_PROJECT_DIR/.swebench/run_tests.sh" > /tmp/test_output.log 2>&1; set -e
    - tail -n 40 /tmp/test_output.log
    # grade by SWE-bench's own parser — pipeline green iff RESOLVED. Run in the
    # miniconda BASE env (py3.11), NOT testbed: swebench 3.x uses `list | None`
    # type hints that TypeError on the testbed's py3.9. grade.py only parses the log.
    - /opt/miniconda3/bin/pip install "swebench==3.0.17" -q
    - /opt/miniconda3/bin/python "$CI_PROJECT_DIR/.swebench/grade.py" /tmp/test_output.log
"""


# ── GitLab API (token from env, never printed) ───────────────────────────────

def _api():
    api = os.environ.get("GITLAB_API", "").rstrip("/")
    tok = os.environ.get("GITLAB_PRIVATE_TOKEN", "")
    if not api or not tok:
        raise SystemExit("set GITLAB_API and GITLAB_PRIVATE_TOKEN in the environment first")
    return api, tok


def _curl_json(method: str, url: str, tok: str, data: dict | None = None) -> dict:
    cmd = ["curl", "-s", "-X", method, "--header", f"PRIVATE-TOKEN: {tok}", url]
    for k, v in (data or {}).items():
        cmd += ["-d", f"{k}={v}"]
    out = subprocess.run(cmd, capture_output=True, text=True).stdout
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {}


def ensure_project(project_path: str) -> int:
    api, tok = _api()
    enc = urllib.parse.quote(project_path, safe="")
    d = _curl_json("GET", f"{api}/projects/{enc}", tok)
    if d.get("id"):
        return d["id"]
    group, name = project_path.split("/", 1)
    g = _curl_json("GET", f"{api}/groups/{group}", tok)
    gid = g.get("id")
    if not gid:
        gid = _curl_json("POST", f"{api}/groups", tok,
                         {"name": group, "path": group, "visibility": "private"}).get("id")
    if not gid:
        raise SystemExit(f"could not ensure group {group!r}")
    p = _curl_json("POST", f"{api}/projects", tok,
                   {"name": name, "path": name, "namespace_id": gid, "visibility": "private"})
    if not p.get("id"):
        raise SystemExit(f"could not create project {project_path!r}: {p.get('message')}")
    return p["id"]


# ── image tree extraction ────────────────────────────────────────────────────

def eval_image_name(instance: dict) -> str:
    name = instance.get("image_name") or instance.get("docker_image")
    if name:
        return name
    iid = instance["instance_id"].replace("__", "_1776_")
    return f"swebench/sweb.eval.x86_64.{iid}:latest".lower()


def extract_base_tree(image: str, dest: Path) -> None:
    """git archive HEAD from the image's /testbed (HEAD tree == base_commit)."""
    dest.mkdir(parents=True, exist_ok=True)
    tar = dest.parent / "tree.tar"
    with open(tar, "wb") as f:
        p = subprocess.run(["docker", "run", "--rm", "--entrypoint", "bash", image,
                            "-c", "cd /testbed && git archive HEAD"], stdout=f)
    if p.returncode != 0:
        raise SystemExit(f"git archive from {image} failed")
    subprocess.run(["tar", "-xf", str(tar), "-C", str(dest)], check=True)
    tar.unlink(missing_ok=True)


# ── main ─────────────────────────────────────────────────────────────────────

def load_instances(subset: str, instance_ids: list[str]) -> list[dict]:
    """Load N instances with ONE dataset read (batch prep re-reads otherwise)."""
    from datasets import load_dataset
    ds = load_dataset(DATASET_MAPPING.get(subset, subset), split="test")
    by_id = {x["instance_id"]: x for x in ds}
    missing = [i for i in instance_ids if i not in by_id]
    if missing:
        raise SystemExit(f"instance(s) {missing!r} not in {subset}")
    return [dict(by_id[i]) for i in instance_ids]


def load_instance(subset: str, instance_id: str) -> dict:
    return load_instances(subset, [instance_id])[0]


def push_options(ci_skip: bool) -> list[str]:
    """`git push` options for the instance-branch push.

    A plain push AUTO-TRIGGERS the branch's baseline pipeline. Building N branches
    in a batch therefore fires N red baselines at once → N webhooks → the
    orchestrator spawns workers unthrottled → OOM + polluted results (the 0:100
    incident). Every caller that triggers pipelines itself (sweep.py, any batch
    prep) must pass ci_skip=True so triggering stays under the driver's
    concurrency control."""
    return ["-o", "ci.skip"] if ci_skip else []


def build_and_push(instance: dict, *, dry_run: bool = False, ci_skip: bool = False) -> str:
    api, tok = _api()
    iid = instance["instance_id"]
    repo = instance["repo"]
    branch = f"instance/{iid}"
    project_path = gitlab_project_path(repo)
    image = eval_image_name(instance)

    with tempfile.TemporaryDirectory(prefix=f"swebench_{iid}_") as td:
        tree = Path(td) / "tree"
        extract_base_tree(image, tree)
        sw = tree / ".swebench"
        sw.mkdir(exist_ok=True)
        inst_json = {
            "instance_id": iid, "image_name": image, "base_commit": instance["base_commit"],
            "problem_statement": instance["problem_statement"],
            "FAIL_TO_PASS": json.loads(instance["FAIL_TO_PASS"]) if isinstance(instance["FAIL_TO_PASS"], str) else instance["FAIL_TO_PASS"],
            "PASS_TO_PASS": json.loads(instance["PASS_TO_PASS"]) if isinstance(instance["PASS_TO_PASS"], str) else instance["PASS_TO_PASS"],
        }
        (sw / "instance.json").write_text(json.dumps(inst_json, indent=2), encoding="utf-8")
        (sw / "test_patch.diff").write_text(instance["test_patch"], encoding="utf-8")
        # full SWE-bench row + grader for the CI's resolved verdict (grade.py reads
        # full_instance.json → make_test_spec + gold FAIL_TO_PASS/PASS_TO_PASS).
        (sw / "full_instance.json").write_text(json.dumps(instance, default=str), encoding="utf-8")
        (sw / "grade.py").write_text((Path(__file__).parent / "grade.py").read_text(), encoding="utf-8")
        (sw / "run_tests.sh").write_text(run_tests_sh(instance), encoding="utf-8")
        (tree / ".gitlab-ci.yml").write_text(
            build_gitlab_ci_yaml(instance, image, branch, instance["test_patch"]), encoding="utf-8")

        # `git init -b <branch>` needs git >= 2.28; Ubuntu 20.04 ships 2.25 and
        # fails with exit 129. `symbolic-ref` on the fresh (commit-less) repo is
        # the portable equivalent — it just points HEAD at the branch we're about
        # to create with the first commit.
        subprocess.run(["git", "init", "-q"], cwd=tree, check=True)
        subprocess.run(["git", "symbolic-ref", "HEAD", f"refs/heads/{branch}"], cwd=tree, check=True)
        subprocess.run(["git", "config", "user.email", "swebench@sdlcma.local"], cwd=tree, check=True)
        subprocess.run(["git", "config", "user.name", "sdlcma"], cwd=tree, check=True)
        subprocess.run(["git", "add", "-A"], cwd=tree, check=True)
        # `.swebench/` gets caught by upstream repo `.gitignore` rules — e.g.
        # seaborn has `*.sw*` (Vim swap files) which matches `.swebench`, so a
        # plain `git add -A` silently skips the whole directory (observed: worker
        # gets 404 on `fetch_file(".swebench/instance.json")`, the branch is built
        # but missing those files). Force-add it to guarantee the descriptor,
        # CI YAML, grader, and test-patch land on the branch regardless of
        # gitignore. (Ignore failure — the directory was just created above, and
        # this is belt-and-suspenders on top of `git add -A`.)
        subprocess.run(["git", "add", "-f", ".swebench/"], cwd=tree,
                       capture_output=True)
        subprocess.run(["git", "commit", "-q", "-m", f"{iid}: base_commit tree + CI + swebench descriptor"],
                       cwd=tree, check=True)
        if dry_run:
            print(f"[dry-run] would push {branch} to {project_path}")
            return branch

        pid = ensure_project(project_path)
        # GitLab makes the first-pushed branch the project default AND protects
        # it — which rejects our idempotent force-push on rebuild. Unprotect the
        # instance branch first (404/no-op when it isn't protected yet).
        _curl_json("DELETE",
                   f"{api}/projects/{pid}/protected_branches/{urllib.parse.quote(branch, safe='')}",
                   tok)
        host = api.split("//", 1)[1].split("/")[0]  # minus:8929
        push_url = f"http://oauth2:{tok}@{host}/{project_path}.git"
        # Force-push: the instance branch is a deterministic snapshot (base_commit
        # tree + CI + descriptors), so re-running setup_instance must overwrite it
        # to exactly this content. The worker never modifies the instance branch
        # (it branches auto/bf OFF it), so force is safe.
        p = subprocess.run(["git", "push", "-q", "--force", *push_options(ci_skip), push_url, branch],
                           cwd=tree, capture_output=True, text=True)
        if p.returncode != 0:
            raise SystemExit(f"push failed: {p.stderr.replace(tok, '***')}")
        # flush: batch prep redirects stdout to a file, where python block-buffers
        # and the operator sees an empty log for minutes and assumes it hung.
        print(f"pushed {branch} → {project_path} (project id={pid})"
              f"{' [ci.skip]' if ci_skip else ''}", flush=True)
    return branch


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--instance", required=True, nargs="+",
                    help="one or more instance_ids, e.g. sympy__sympy-22914")
    ap.add_argument("--subset", default="verified")
    ap.add_argument("--dry-run", action="store_true", help="build the branch locally, don't push")
    ap.add_argument("--ci-skip", action="store_true",
                    help="push with `-o ci.skip` so the baseline pipeline is NOT auto-triggered. "
                         "Use for batch prep — otherwise N branches fire N simultaneous baselines "
                         "and the orchestrator spawns workers unthrottled.")
    args = ap.parse_args()
    for inst in load_instances(args.subset, args.instance):
        build_and_push(inst, dry_run=args.dry_run, ci_skip=args.ci_skip)


if __name__ == "__main__":
    main()
