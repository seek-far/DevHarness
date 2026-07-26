"""Tests for the ver==99 GitLab sweep infra (setup_instance + sweep).

Pure-logic only (no docker / GitLab / stack): CI-recipe generation, test-file
extraction, gitlab project path mapping, and journal resolved-rate aggregation.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_INFRA = Path(__file__).resolve().parents[1] / "infra" / "swebench-gitlab"


def _load(mod_name):
    spec = importlib.util.spec_from_file_location(mod_name, _INFRA / f"{mod_name}.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = m
    spec.loader.exec_module(m)
    return m


si = _load("setup_instance")


# ── setup_instance ────────────────────────────────────────────────────────────

def test_gitlab_project_path():
    assert si.gitlab_project_path("sympy/sympy") == "swebench/sympy"
    assert si.gitlab_project_path("django/django") == "swebench/django"


def test_eval_image_name_derivation():
    assert si.eval_image_name({"instance_id": "sympy__sympy-22914"}) == \
        "swebench/sweb.eval.x86_64.sympy_1776_sympy-22914:latest"
    assert si.eval_image_name({"instance_id": "x", "image_name": "custom:tag"}) == "custom:tag"


def test_eval_test_cmd_extracts_between_markers(monkeypatch):
    class _TS:
        eval_script_list = [
            "conda activate testbed", "cd /testbed",
            ": '>>>>> Start Test Output'",
            "pytest -rA astropy/nddata/tests/test_x.py",
            ": '>>>>> End Test Output'",
            "git checkout base test.py",
        ]
    monkeypatch.setattr(
        "swebench.harness.test_spec.test_spec.make_test_spec",
        lambda inst: _TS(), raising=False)
    cmd = si.eval_test_cmd({"instance_id": "astropy__astropy-1"})
    assert cmd == "pytest -rA astropy/nddata/tests/test_x.py"


def test_build_ci_yaml_grades_via_swebench_parser(monkeypatch):
    # ANY runner works (no exit-code assumption): run_tests.sh runs the authoritative
    # command → log, then grade.py (swebench's parser) gates the pipeline.
    monkeypatch.setattr(si, "eval_test_cmd",
                        lambda inst: "./tests/runtests.py --settings=test_sqlite i18n.tests")
    inst = {"instance_id": "django__django-15098", "repo": "django/django"}
    yaml = si.build_gitlab_ci_yaml(inst, "img:latest", "instance/django__django-15098", "")
    assert "image: img:latest" in yaml
    assert 'FROM_REF: "instance/django__django-15098"' in yaml
    assert "conda activate testbed" in yaml
    assert 'git diff "origin/${FROM_REF}...HEAD"' in yaml
    assert ".swebench/test_patch.diff" in yaml
    assert "pip install -e ." in yaml
    # UTF-8 locale so non-ASCII test output (e.g. django's '…') doesn't crash
    # the run on the runner's ascii default (else PASS_TO_PASS falsely all-fail)
    assert 'LC_ALL: "C.UTF-8"' in yaml and 'PYTHONIOENCODING: "utf-8"' in yaml
    # determinism env: prevents false-green baselines on order-sensitive instances
    assert 'PYTHONHASHSEED: "0"' in yaml and 'PYTHONUNBUFFERED: "1"' in yaml
    # test run is delegated to the committed script (no inline quoting in YAML)
    assert 'bash "$CI_PROJECT_DIR/.swebench/run_tests.sh"' in yaml
    # graded by swebench's own parser; the LAST step is grade.py (gates green/red)
    assert 'pip install "swebench==3.0.17"' in yaml
    assert yaml.rstrip().endswith(".swebench/grade.py\" /tmp/test_output.log")


def test_run_tests_sh_wraps_cmd_in_markers_quote_safe(monkeypatch):
    # sympy's command has single quotes (PYTHONWARNINGS='...') — it lives in a
    # script file, never inlined into YAML, so quoting can't break the pipeline.
    monkeypatch.setattr(si, "eval_test_cmd",
                        lambda inst: "PYTHONWARNINGS='ignore::UserWarning' bin/test -C x.py")
    sh = si.run_tests_sh({"instance_id": "sympy__sympy-22914"})
    assert ">>>>> Start Test Output" in sh and ">>>>> End Test Output" in sh
    assert "PYTHONWARNINGS='ignore::UserWarning' bin/test -C x.py" in sh
    # markers bracket the command
    lines = [l for l in sh.splitlines() if l.strip()]
    assert lines[-3].endswith("Start Test Output\"")
    assert lines[-1].endswith("End Test Output\"")


def test_grade_is_offline_no_make_test_spec(tmp_path):
    # make_test_spec() downloads the repo's requirements from raw.githubusercontent
    # (django et al.) — a network dependency in the CI hot path (4-5 min stall or
    # a hard ConnectionError on hosts without GitHub access). Grading is pure log
    # parsing: only .repo/.version are read off the spec.
    grade = _load("grade")
    assert not hasattr(grade, "make_test_spec")

    ts = grade._SpecShim({"repo": "django/django", "version": "3.0",
                          "instance_id": "django__django-11149"})
    assert (ts.repo, ts.version) == ("django/django", "3.0")

    # the shim is enough for swebench's own parser
    log = tmp_path / "out.log"
    log.write_text(
        ">>>>> Start Test Output\n"
        "test_a (admin_inlines.tests.TestInline) ... ok\n"
        "test_b (admin_inlines.tests.TestInline) ... FAIL\n"
        ">>>>> End Test Output\n"
    )
    status_map, ok = grade.get_logs_eval(ts, str(log))
    assert status_map  # parsed something rather than raising on the shim


def test_push_options_ci_skip():
    # A plain push auto-triggers the baseline pipeline; batch prep must suppress
    # that or N branches fire N simultaneous baselines (the 0:100 flood).
    assert si.push_options(True) == ["-o", "ci.skip"]
    assert si.push_options(False) == []


def test_git_init_uses_symbolic_ref_not_dash_b():
    # `git init -b` needs git >= 2.28; Ubuntu 20.04 ships 2.25 (exit 129).
    src = (_INFRA / "setup_instance.py").read_text()
    assert '"git", "init", "-q", "-b"' not in src
    assert '"git", "symbolic-ref", "HEAD"' in src


# ── sweep: journal aggregation ────────────────────────────────────────────────

def test_journal_verdict_and_resolved(tmp_path, monkeypatch):
    sweep = _load("sweep")
    jdir = tmp_path / "journal"
    (jdir / "20260721_swebench-sympy-22914_langgraph_deepseek").mkdir(parents=True)
    (jdir / "20260721_swebench-sympy-22914_langgraph_deepseek" / "record.json").write_text(
        json.dumps({"bug_id": "swebench-sympy-22914", "swebench_instance_id": "sympy__sympy-22914",
                    "outcome": "fixed", "resolved": True, "llm_call_count": 37})
    )
    monkeypatch.setattr(sweep, "JOURNAL_DIR", jdir)

    rec = sweep.journal_verdict("sympy__sympy-22914")
    assert rec is not None and rec["resolved"] is True
    assert sweep.is_done("sympy__sympy-22914") is True
    assert sweep.is_done("sympy__sympy-99999") is False
    assert sweep.journal_verdict("sympy__sympy-99999") is None


def test_journal_verdict_prefers_newest(tmp_path, monkeypatch):
    sweep = _load("sweep")
    jdir = tmp_path / "j"
    for i, resolved in enumerate([False, True]):
        d = jdir / f"2026072{i}_b_langgraph_m"
        d.mkdir(parents=True)
        (d / "record.json").write_text(json.dumps(
            {"bug_id": "b", "swebench_instance_id": "x__y-1", "outcome": "fixed", "resolved": resolved}))
        import os, time
        os.utime(d / "record.json", (1000 + i, 1000 + i))
    monkeypatch.setattr(sweep, "JOURNAL_DIR", jdir)
    assert sweep.journal_verdict("x__y-1")["resolved"] is True  # newest wins


def test_journal_verdict_since_ignores_the_previous_run(tmp_path, monkeypatch):
    """A re-run must not read the PREVIOUS run's record.

    Without a `since` floor the driver sees a terminal verdict the instant it
    triggers, "completes" every instance in milliseconds and reports stale
    results — which looks like a successful sweep.
    """
    import os
    sweep = _load("sweep")
    jdir = tmp_path / "j"
    d = jdir / "old_entry"
    d.mkdir(parents=True)
    (d / "record.json").write_text(json.dumps(
        {"bug_id": "b", "swebench_instance_id": "x__y-1", "outcome": "fixed", "resolved": False}))
    os.utime(d / "record.json", (1000, 1000))
    monkeypatch.setattr(sweep, "JOURNAL_DIR", jdir)

    assert sweep.journal_verdict("x__y-1") is not None          # no floor: visible
    assert sweep.is_done("x__y-1") is True
    assert sweep.journal_verdict("x__y-1", since=2000) is None  # triggered later: invisible
    assert sweep.is_done("x__y-1", since=2000) is False


def test_sweep_cli_exposes_concurrency_and_skip_setup():
    sweep = _load("sweep")
    import inspect
    src = inspect.getsource(sweep.main)
    assert "--concurrency" in src and "--skip-setup" in src
    # trigger instant must be captured BEFORE the trigger call, or a fast worker
    # could write its record before we sample the clock and be filtered out.
    assert src.index("triggered_at = time.time()") < src.index("trigger_baseline_pipeline(inst)")


def test_eval_test_cmd_is_offline_no_github_download():
    """setup_instance.eval_test_cmd must NOT hit raw.githubusercontent.com.
    make_test_spec builds an env-setup script that downloads the repo's
    environment.yml/requirements from GitHub — a network dep that ConnectionResets
    on a CN host without GitHub (branch-build failures observed with the exit-node
    off). eval_test_cmd only needs the test COMMAND (static MAP_REPO_VERSION_TO_SPECS),
    so it stubs those two downloads. Same offline principle as grade.py's _SpecShim."""
    src = (_INFRA / "setup_instance.py").read_text()
    assert "get_environment_yml_by_commit = lambda" in src
    assert "get_requirements_by_commit = lambda" in src
    # the stub must be installed BEFORE make_test_spec is called
    assert src.index("get_environment_yml_by_commit = lambda") < src.index("ts = make_test_spec(instance)")
