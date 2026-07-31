"""Tests for the L2c concurrent-chaos harness (chaos_kill + verify_l2c).

Pure logic only — no docker, no GitLab, no stack. What these pin down is the
part of L2c that is easy to get silently wrong: the kill pattern must match the
worker the spawner actually creates (a pattern that matches nothing looks
exactly like "chaos never fired"), the image->instance mapping the whole
per-instance comparison keys on, and the assertion logic itself, which must fail
on a wrong-container re-attach rather than quietly passing.
"""
from __future__ import annotations

import importlib.util
import json
import re
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


ck = _load("chaos_kill")
vl = _load("verify_l2c")


# ── chaos_kill ────────────────────────────────────────────────────────────────

def test_instance_of_image():
    assert ck.instance_of(
        "docker.io/swebench/sweb.eval.x86_64.astropy_1776_astropy-12907:latest"
    ) == "astropy__astropy-12907"
    assert ck.instance_of("swebench/sweb.eval.arm64.django_1776_django-11066:v1") == \
        "django__django-11066"
    assert ck.instance_of(None) is None
    assert ck.instance_of("redis:latest") is None


def test_kill_pattern_matches_the_worker_the_spawner_creates():
    """The pattern is only useful if it matches the real argv.

    orchestrator/spawner.py execs `<python> <repo>/bf_worker/bf_worker.py
    --bug-id <bug_id>`; a rename there must break this test, not the run.
    """
    from orchestrator.spawner import WORKER_SCRIPT

    bug_id = "2026_07_30-05_45_53_6_e3f7"
    argv = f"/usr/bin/python3 {WORKER_SCRIPT} --bug-id {bug_id}"
    assert ck.kill_pattern(bug_id) in argv
    # and it must not match a neighbouring run at the same concurrency
    assert ck.kill_pattern("2026_07_30-05_45_53_6_aaaa") not in argv


def test_should_fire_only_at_or_past_the_target_step():
    assert ck.should_fire({"step": 6}, 6)
    assert ck.should_fire({"step": 9}, 6)
    assert not ck.should_fire({"step": 5}, 6)
    assert not ck.should_fire(None, 6)
    assert not ck.should_fire({}, 1)
    assert not ck.should_fire({"step": "nonsense"}, 1)


def test_plan_is_deterministic_for_a_seed_and_the_doom_draw_is_deferred():
    """The draw happens up front (reproducible from the seed) but is only
    *applied* once `env.json` names the instance — chaos may need to skip runs
    outside the judgeable subset."""
    import random

    a = [ck.plan_for(random.Random(7), 0.34, 2, 6) for _ in range(5)]
    b = [ck.plan_for(random.Random(7), 0.34, 2, 6) for _ in range(5)]
    assert a == b
    assert all(2 <= p["target"] <= 6 for p in a)
    assert all(p["doomed"] is None for p in a), "undecided until the instance is known"
    assert not any(ck.decide_doom(p, "i1", rate=0.0, only=None) for p in a)
    assert all(ck.decide_doom(p, "i1", rate=1.0, only=None) for p in a)


# ── verify_l2c ────────────────────────────────────────────────────────────────

def _events():
    return [
        {"kind": "start"},
        {"kind": "observe", "run_key": "b1", "doomed": True, "target_step": 3},
        {"kind": "observe", "run_key": "b2", "doomed": False, "target_step": 4},
        {"kind": "env", "run_key": "b1", "container_id": "c1",
         "image": "swebench/sweb.eval.x86_64.astropy_1776_astropy-12907:latest",
         "instance": "astropy__astropy-12907"},
        {"kind": "env", "run_key": "b2", "container_id": "c2",
         "image": "swebench/sweb.eval.x86_64.django_1776_django-11066:latest",
         "instance": "django__django-11066"},
        {"kind": "kill", "run_key": "b1", "instance": "astropy__astropy-12907",
         "container_id": "c1", "step": 3, "env_seq": 3},
        {"kind": "disk", "bytes": 1_000_000, "containers": 2},
        {"kind": "disk", "bytes": 3_000_000, "containers": 5},
        {"kind": "purge", "run_key": "b1"},
    ]


def test_index_events_splits_the_stream():
    ix = vl.index_events(_events())
    assert set(ix["kills"]) == {"b1"}
    assert set(ix["envs"]) == {"b1", "b2"}
    assert set(ix["observed"]) == {"b1", "b2"}
    assert ix["purged"] == {"b1"}
    assert ix["disk_peak"] == 3_000_000
    assert ix["container_peak"] == 5


def test_effective_kills_excludes_one_that_landed_after_the_loop():
    """A kill arriving after mini released its records did not test resume.

    Scoring it as "resume failed" would make the arm's verdict depend on where
    the dice landed; scoring it silently as a pass would hide a real failure.
    It is measured (`record_after_kill`) and reported separately.
    """
    kills = {
        "b1": {"instance": "i1", "record_after_kill": "present"},
        "b2": {"instance": "i2", "record_after_kill": "gone"},
        "b3": {"instance": "i3"},                      # older event file: conservative
    }
    assert set(vl.effective_kills(kills)) == {"b1", "b3"}


def test_a_missed_kill_does_not_demand_an_attach_line():
    kills = {"b2": {"instance": "i2", "container_id": "c2", "record_after_kill": "gone"}}
    assert vl.attach_mismatches(vl.effective_kills(kills), attach={}) == []


def test_parse_attach_extracts_key_and_container():
    log = (
        "2026-07-30 21:30:00 INFO resume: attached to container abc123 "
        "(key=2026_07_30-05_45_53_6_e3f7, marker=True)\n"
        "unrelated line\n"
        "2026-07-30 21:31:00 INFO resume: attached to container def456 "
        "(key=2026_07_30-05_46_00_1_aaaa, marker=False)\n"
    )
    attach = vl.parse_attach(log)
    assert attach == {"2026_07_30-05_45_53_6_e3f7": ["abc123"],
                      "2026_07_30-05_46_00_1_aaaa": ["def456"]}
    assert vl.parse_attach(log.splitlines()) == attach       # any iterable of lines


def test_the_orchestrator_log_is_streamed_never_loaded(tmp_path, monkeypatch):
    """A 100-instance L3 run leaves a 12 GB orch.log.

    `read_text()` on it allocated the whole file and got the verifier
    OOM-killed on a swapless host — and the 12 GB cold read took the box's sshd
    with it. Loading this file must stay impossible, not merely discouraged.
    """
    p = tmp_path / "orch.log"
    p.write_text("resume: attached to container abc123 (key=b1, marker=True)\n"
                 "noise\n")

    def explode(*a, **k):
        raise AssertionError("orch.log must never be read into memory whole")

    monkeypatch.setattr(Path, "read_text", explode)
    monkeypatch.setattr(Path, "read_bytes", explode)
    assert vl.parse_attach_file(p) == {"b1": ["abc123"]}
    assert vl.parse_attach_file(tmp_path / "absent.log") == {}   # no log is fine


def _rec(iid, patch, **kw):
    base = {"swebench_instance_id": iid, "model_patch": patch, "outcome": "fixed",
            "resolved": True, "llm_call_count": 13, "bug_id": kw.pop("bug_id", None)}
    base.update(kw)
    return base


def _patch_of(rec):
    return rec.get("model_patch")


def test_compare_arms_clean_run():
    ctrl = {"i1": _rec("i1", "PATCH"), "i2": _rec("i2", "OTHER")}
    chaos = {"i1": _rec("i1", "PATCH", step_resume_count=1, step_resumed_from_step=3,
                        step_replayed_command_count=1),
             "i2": _rec("i2", "OTHER")}
    out = vl.compare_arms(ctrl, chaos, killed_iids={"i1"}, patch_of=_patch_of)
    assert out["patch_bad"] == [] and out["meta_bad"] == [] and out["no_resume"] == []
    row = next(r for r in out["rows"] if r["instance"] == "i1")
    assert row["patch"] == "SAME" and row["killed"] is True and row["resume"] == 1


def test_compare_arms_flags_a_diverged_patch_and_a_kill_that_never_resumed():
    ctrl = {"i1": _rec("i1", "PATCH"), "i2": _rec("i2", "X")}
    chaos = {"i1": _rec("i1", "DIFFERENT", step_resume_count=1),
             # killed, but resume never happened -> patch equality proves nothing
             "i2": _rec("i2", "X", step_resume_count=0)}
    out = vl.compare_arms(ctrl, chaos, killed_iids={"i1", "i2"}, patch_of=_patch_of)
    assert out["patch_bad"] == ["i1"]
    assert out["no_resume"] == ["i2"]


def test_compare_arms_flags_a_missing_record():
    out = vl.compare_arms({"i1": _rec("i1", "P")}, {}, killed_iids=set(), patch_of=_patch_of)
    assert out["meta_bad"] and out["rows"][0]["patch"] == "MISSING"


def test_an_unavailable_patch_is_never_reported_as_identical():
    """The trap this check fell into once: `record["model_patch"]` does not
    exist, so both sides read "" and every instance compared equal. An absent
    patch must surface as N/A + SKIP, never as SAME + PASS."""
    ctrl = {"i1": _rec("i1", None)}
    chaos = {"i1": _rec("i1", None)}
    out = vl.compare_arms(ctrl, chaos, killed_iids=set(), patch_of=_patch_of)
    assert out["patch_bad"] == []
    assert out["patch_na"] and out["rows"][0]["patch"] == "N/A"
    verdicts = dict((n, v) for n, v, _ in vl.build_checks(out, [], [], [], [], [], ""))
    assert verdicts["patch identical to the control arm"] == vl.SKIP


def test_one_sided_patch_is_unavailable_not_a_difference():
    """A crashed chaos run has no fix branch at all: that is 'cannot compare',
    and the outcome check is what fails it."""
    out = vl.compare_arms({"i1": _rec("i1", "P")}, {"i1": _rec("i1", None)},
                          killed_iids={"i1"}, patch_of=_patch_of)
    assert out["patch_bad"] == [] and len(out["patch_na"]) == 1


# ── the produced patch comes from GitLab, not the RunRecord ──────────────────

def test_project_path_from_web_url():
    assert vl.project_path({"project_web_url": "http://ls4900/swebench/astropy"}) == \
        "swebench/astropy"
    assert vl.project_path({"project_web_url": "https://gitlab.com/g/sub/proj/"}) == "g/sub/proj"
    assert vl.project_path({}) is None


def test_normalized_diff_is_order_independent():
    a = {"diffs": [{"old_path": "b.py", "new_path": "b.py", "diff": "@@ -1 +1 @@\n-x\n+y\n"},
                   {"old_path": "a.py", "new_path": "a.py", "diff": "@@ -2 +2 @@\n-p\n+q\n"}]}
    b = {"diffs": list(reversed(a["diffs"]))}
    assert vl.normalized_diff(a) == vl.normalized_diff(b)
    assert "a.py" in vl.normalized_diff(a) and "+y" in vl.normalized_diff(a)


def test_fetch_fix_diff_returns_none_without_credentials_or_branch():
    rec = {"project_web_url": "http://h/g/p", "commit_branch": "auto/bf/x", "base_branch": "instance/i"}
    assert vl.fetch_fix_diff(rec, api="", token="") is None
    assert vl.fetch_fix_diff({"project_web_url": "http://h/g/p"}, api="http://h/api/v4",
                             token="t") is None


def test_diff_cache_round_trips_and_tolerates_a_missing_or_corrupt_file(tmp_path):
    p = tmp_path / "sub" / "diffs.json"
    assert vl.load_diff_cache(p) == {}          # not there yet
    assert vl.load_diff_cache(None) == {}       # feature switched off
    vl.save_diff_cache(p, {"b1:auto/bf/x": "PATCH"})
    assert vl.load_diff_cache(p) == {"b1:auto/bf/x": "PATCH"}
    p.write_text("{ truncated")                 # died mid-write in an older run
    assert vl.load_diff_cache(p) == {}


def test_a_failed_fetch_is_not_frozen_into_the_diff_cache(tmp_path):
    """GitLab unreachable ≠ "this run produced no patch".

    The cache exists because the host that can reach GitLab is the one that
    keeps dying. If a failed fetch were persisted, one outage would turn every
    later re-run — on any host — into a permanent SKIP.
    """
    p = tmp_path / "diffs.json"
    rec = {"bug_id": "b1", "commit_branch": "auto/bf/x"}
    fetcher = vl.make_patch_fetcher("api", "tok", {}, p, fetch=lambda *_a: None)
    assert fetcher(rec) is None
    assert not p.exists()                       # nothing persisted
    assert vl.load_diff_cache(p) == {}


def test_a_fetched_patch_is_persisted_immediately_and_reused_across_arms(tmp_path):
    """Written per patch, not at the end — the host it runs on keeps dying."""
    p = tmp_path / "diffs.json"
    calls = []

    def fake(rec, api, token):
        calls.append(rec["bug_id"])
        return "PATCH-" + rec["bug_id"]

    cache: dict = {}
    f1 = vl.make_patch_fetcher("api", "tok", cache, p, fetch=fake)
    assert f1({"bug_id": "b1", "commit_branch": "br"}) == "PATCH-b1"
    assert vl.load_diff_cache(p) == {"b1:br": "PATCH-b1"}   # already on disk

    # a second table (A vs C) re-reads the same control arm: no second request
    f2 = vl.make_patch_fetcher("api", "tok", vl.load_diff_cache(p), p, fetch=fake)
    assert f2({"bug_id": "b1", "commit_branch": "br"}) == "PATCH-b1"
    assert calls == ["b1"]


def test_pacing_applies_to_fetches_but_never_to_cache_hits(monkeypatch, tmp_path):
    """A cache hit costs the server nothing, so it must not cost wall time."""
    slept = []
    monkeypatch.setattr(vl.time, "sleep", lambda s: slept.append(s))
    f = vl.make_patch_fetcher("api", "tok", {}, tmp_path / "d.json", delay=0.5,
                              fetch=lambda rec, a, t: "P")
    rec = {"bug_id": "b1", "commit_branch": "br"}
    f(rec)
    assert slept == [0.5]
    f(rec)                                       # served from cache
    assert slept == [0.5]


def test_pacing_is_off_by_default():
    """The knob must not change behaviour for anyone who does not pass it."""
    slept = []
    f = vl.make_patch_fetcher("api", "tok", {}, None,
                              fetch=lambda rec, a, t: "P")
    import time as _t
    t0 = _t.monotonic()
    f({"bug_id": "b1", "commit_branch": "br"})
    assert _t.monotonic() - t0 < 0.1
    assert slept == []


def test_attach_mismatch_catches_the_wrong_container():
    kills = {"b1": {"container_id": "c1", "instance": "i1"}}
    assert vl.attach_mismatches(kills, {"b1": ["c1"]}) == []
    assert vl.attach_mismatches(kills, {"b1": ["c9"]})          # attached elsewhere
    assert vl.attach_mismatches(kills, {})                      # never attached at all


def test_container_crosstalk_detects_two_runs_on_one_container():
    assert vl.container_crosstalk({"b1": ["c1"], "b2": ["c2"]}) == []
    bad = vl.container_crosstalk({"b1": ["c1"], "b2": ["c1"]})
    assert bad and bad[0][0] == "c1"


def test_isolation_breaks_detects_a_record_under_the_wrong_run_key():
    envs = {"b1": {"instance": "astropy__astropy-12907"}}
    ok = [{"bug_id": "b1", "swebench_instance_id": "astropy__astropy-12907"}]
    assert vl.isolation_breaks(envs, ok) == []
    swapped = [{"bug_id": "b1", "swebench_instance_id": "django__django-11066"}]
    assert vl.isolation_breaks(envs, swapped)


def test_build_checks_verdicts():
    clean = {"patch_bad": [], "meta_bad": [], "no_resume": []}
    checks = vl.build_checks(clean, [], [], [], [], [], "")
    assert all(v == vl.OK for _, v, _ in checks)
    leaky = vl.build_checks(clean, [], [], [], [], [], "abc123 Up 2 minutes")
    assert dict((n, v) for n, v, _ in leaky)["no leaked mini containers"] == vl.BAD


def test_journal_records_windows_by_mtime(tmp_path):
    import os
    import time

    jd = tmp_path / "journal"
    for name, iid in (("old", "i_old"), ("new", "i_new")):
        d = jd / name
        d.mkdir(parents=True)
        (d / "record.json").write_text(json.dumps(
            {"swebench_instance_id": iid, "model_patch": "P"}))
    now = time.time()
    os.utime(jd / "old" / "record.json", (now - 10_000, now - 10_000))
    got = vl.journal_records(jd, now - 60, now + 60)
    assert set(got) == {"i_new"}


def test_journal_records_keeps_the_newest_record_per_instance(tmp_path):
    """A killed run writes its journal entry after the restart; the arm window
    can contain both a stale and a fresh entry for the same instance."""
    import os
    import time

    jd = tmp_path / "journal"
    now = time.time()
    for name, calls, age in (("first", 5, 300), ("second", 13, 10)):
        d = jd / name
        d.mkdir(parents=True)
        (d / "record.json").write_text(json.dumps(
            {"swebench_instance_id": "i1", "llm_call_count": calls}))
        os.utime(d / "record.json", (now - age, now - age))
    got = vl.journal_records(jd, now - 600, now)
    assert got["i1"]["llm_call_count"] == 13


# ── L3 scale: cache hit rate, judgeable subset, targeted chaos ───────────────

ch = _load("cache_hitrate")

_GW_LOG = """\
2026-07-30 22:48:44,965 INFO [gw llm_gateway.app:chat_completions:312] phase_marker \
phase=cache_lookup mode=cache result=hit key=c663 bug_id=astropy__astropy-14096 hit_count=6
2026-07-30 22:48:45,001 INFO [gw llm_gateway.app:chat_completions:312] phase_marker \
phase=cache_lookup mode=cache result=miss key=e13e bug_id=django__django-11095
2026-07-30 22:48:46,002 INFO [gw llm_gateway.app:chat_completions:312] phase_marker \
phase=cache_lookup mode=cache result=hit key=7cb5 bug_id=django__django-11095
2026-07-30 23:99:99,000 INFO garbage line that must not crash the parser
"""


def test_parse_gateway_cache_log():
    rows = ch.parse_log(_GW_LOG)
    assert [(r[1], r[2]) for r in rows] == [
        ("hit", "astropy__astropy-14096"),
        ("miss", "django__django-11095"),
        ("hit", "django__django-11095"),
    ]


def test_per_instance_windows_and_rates():
    rows = ch.parse_log(_GW_LOG)
    start = rows[0][0]
    stats = ch.per_instance(rows, start, start + 5)
    assert stats["astropy__astropy-14096"] == {"hit": 1, "miss": 0, "total": 1, "rate": 1.0}
    assert stats["django__django-11095"]["miss"] == 1
    assert ch.per_instance(rows, start + 3600, start + 7200) == {}


def test_clean_subset_requires_a_full_replay_in_every_arm():
    per_arm = {
        "A": {"i1": {"total": 5, "miss": 0}, "i2": {"total": 4, "miss": 0},
              "i3": {"total": 3, "miss": 1}},
        "A2": {"i1": {"total": 5, "miss": 0}, "i2": {"total": 4, "miss": 2},
               "i3": {"total": 3, "miss": 0}},
    }
    assert ch.clean_subset(per_arm) == ["i1"]
    # an instance absent from one arm is not judgeable either
    assert ch.clean_subset({"A": {"i1": {"total": 5, "miss": 0}}, "A2": {}}) == []


def test_chaos_targets_only_the_judgeable_subset():
    """Killing outside the subset spends the chaos budget on unusable cells."""
    plan = {"roll": 0.0, "target": 3}          # roll always under the rate
    assert ck.decide_doom(plan, "i1", rate=0.5, only={"i1"}) is True
    assert ck.decide_doom(plan, "i2", rate=0.5, only={"i1"}) is False
    assert ck.decide_doom(plan, None, rate=0.5, only={"i1"}) is False
    assert ck.decide_doom(plan, "i2", rate=0.5, only=None) is True
    assert ck.decide_doom({"roll": 0.9, "target": 3}, "i1", rate=0.5, only=None) is False


def test_subset_bias_line_reports_both_distributions():
    judgeable = {"i1": {"llm_call_count": 8}, "i2": {"llm_call_count": 12}}
    everything = dict(judgeable, i3={"llm_call_count": 115})
    line = vl.subset_bias(judgeable, everything)
    assert "n=2" in line and "n=3" in line and "115" in line


def test_cache_snapshot_script_uses_the_flags_the_transfer_tool_actually_has():
    """The export flag is `--out`; `--to` fails.

    That typo cost a whole control arm: the wrapper printed "snapshot written",
    the file did not exist, the next arm's restore failed, and — because neither
    step checked its exit code — the arm ran anyway, without cache isolation.
    So this pins both halves: the flag, and the fact that a failure aborts.
    """
    import subprocess

    script = (_INFRA / "llm_cache_arm_reset.sh").read_text()
    help_out = subprocess.run(
        [sys.executable, "-m", "tools.llm_cache_transfer", "export", "--help"],
        capture_output=True, text=True,
        cwd=str(Path(__file__).resolve().parents[1])).stdout
    for flag in re.findall(r"--[a-z-]+", script.split("export)")[1].split(";;")[0]):
        if flag in ("--out", "--db", "--to"):
            assert flag in help_out, f"{flag} is not a flag of llm_cache_transfer export"
    assert "|| {" in script, "a failed export must abort, not print success"

    runner = (_INFRA / "run_l2c.sh").read_text()
    assert "refusing to run arm" in runner, "a failed restore must abort the arm"


def test_cache_restore_sources_credentials_and_verifies_the_gateway_came_back():
    """Restoring the cache restarts the gateway — from a bare environment.

    Without the backend credentials it exits at startup; without a fatal health
    check nobody notices until a whole arm has run against a dead port and
    reported errors that look like a product failure. Both halves cost a real
    arm on ls4900.
    """
    script = (_INFRA / "llm_cache_arm_reset.sh").read_text()
    restore = script.split("restore)")[1].split(";;")[0]
    assert "LLM_ENV_FILE" in restore, "restore must source the backend credentials"
    assert "exit 1" in restore and "did NOT come back" in restore, \
        "a gateway that fails to restart must be fatal, not printed"


@pytest.mark.parametrize("script", ["run_l2c.sh", "ver99_stack_up.sh",
                                    "llm_cache_arm_reset.sh"])
def test_shell_drivers_are_valid_bash(script):
    import subprocess

    assert subprocess.run(["bash", "-n", str(_INFRA / script)]).returncode == 0
